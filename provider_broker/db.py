import hashlib
import hmac
import json
import math
import sqlite3
from contextlib import nullcontext
from importlib.metadata import PackageNotFoundError, version
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog import (
    APPROVED_MODEL_IDS,
    APPROVED_STAGE_MODELS,
    BROKER_PROVIDER_MODELS,
    OFFICIAL_PRICE_SNAPSHOT,
    OFFICIAL_PROVIDER_SNAPSHOTS,
    canonicalize,
    normalize_hostname,
)
from .pricing import DEFAULT_OUTPUT_PRICE_CNY, PROVIDER_TYPES, canonical_provider_type, require_provider_type


TELEMETRY_SCHEMA_VERSION = 1
ROUTING_POLICY_VERSION = "v1"
DELIVERY_MODES = {"non_stream", "plain_stream", "validated_stream"}
PRICING_MIGRATION_VERSION = 4
ACCOUNTING_WINDOWS = {"1h": "-1 hour", "24h": "-24 hours", "7d": "-7 days", "30d": "-30 days"}
API_KEY_RESOURCE_FIELDS = frozenset({
    "fingerprint", "normalized_hostname", "status", "note", "api_key_mask",
    "max_parallel", "window", "total_tokens", "fee_buckets", "edit", "models",
})
STAGE_RESOURCE_FIELDS = frozenset({
    "stage", "fingerprint", "note", "model", "family", "provider_type",
    "normalized_hostname", "api_key_mask", "status", "max_parallel",
    "callable", "latest_test", "technical_success_rate",
    "avg_first_token_latency_ms", "window", "total_tokens", "fee_buckets", "edit",
})

# The management console intentionally exposes the three routing stages, not
# every model discovered in a provider's inventory.  Runtime routing may still
# use the wider canonical catalog.
STAGE_MODELS = APPROVED_STAGE_MODELS


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
    site_id: str = "default"
    wire_model: str | None = None
    model_aliases: dict[str, str] | None = None
    pricing_by_model: dict[str, dict] | None = None
    price_currency: str | None = None
    price_comparable: bool = False
    price_source: str | None = None
    price_reason: str | None = None


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

    def _upgrade_pricing_provider_schema(self) -> None:
        """Broaden the old CHECK constraint without changing provider IDs."""
        row = self.conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pricing_provider'"
        ).fetchone()
        if row is None or "'openai'" in (row[0] or ''):
            return
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            self.conn.execute("""CREATE TABLE pricing_provider_v4 (
                id INTEGER PRIMARY KEY,
                provider_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                provider_type TEXT NOT NULL CHECK(provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra','official','direct','relay','legacy-migration')),
                multiplier REAL NOT NULL DEFAULT 1.0 CHECK(multiplier > 0),
                active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
            )""")
            self.conn.execute("INSERT INTO pricing_provider_v4 SELECT id,provider_key,name,provider_type,multiplier,active FROM pricing_provider")
            self.conn.execute("DROP TABLE pricing_provider")
            self.conn.execute("ALTER TABLE pricing_provider_v4 RENAME TO pricing_provider")
            self.conn.commit()
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def _migrate(self):
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS source_provider (
          id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          base_url TEXT NOT NULL, api_key BLOB NOT NULL, provider_type TEXT NOT NULL,
          pricing_provider_type TEXT,
          request_headers BLOB, api_key_mask TEXT NOT NULL DEFAULT '***',
          models_json TEXT NOT NULL, source_json TEXT NOT NULL, synced_at TEXT NOT NULL,
          pricing_provider_id INTEGER REFERENCES pricing_provider(id)
        );
        CREATE TABLE IF NOT EXISTS policy (
          fingerprint TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1,
          price_group INTEGER NOT NULL DEFAULT 100, multiplier REAL NOT NULL DEFAULT 1.0, calibrated INTEGER NOT NULL DEFAULT 0, tiers_json TEXT NOT NULL DEFAULT '["standard","smart","expert"]'
        );
        CREATE TABLE IF NOT EXISTS observation (
          id INTEGER PRIMARY KEY, fingerprint TEXT NOT NULL, requested_model TEXT NOT NULL,
          actual_model TEXT, tier TEXT NOT NULL, effort TEXT, success INTEGER NOT NULL,
          latency_ms REAL, error TEXT, currency TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS model_catalog (
          model TEXT PRIMARY KEY, family TEXT NOT NULL, intellect TEXT NOT NULL,
          input_price REAL NOT NULL, cache_price REAL NOT NULL, output_price REAL NOT NULL,
          currency TEXT NOT NULL DEFAULT 'USD'
        );
        CREATE TABLE IF NOT EXISTS pricing_provider (
          id INTEGER PRIMARY KEY,
          provider_key TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          provider_type TEXT NOT NULL CHECK(provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra','official','direct','relay','legacy-migration')),
          multiplier REAL NOT NULL DEFAULT 1.0 CHECK(multiplier > 0),
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS canonical_model (
          id TEXT PRIMARY KEY,
          stage TEXT NOT NULL CHECK(stage IN ('standard','smart','expert')),
          family TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        CREATE TABLE IF NOT EXISTS provider_model_price (
          id INTEGER PRIMARY KEY,
          provider_id INTEGER NOT NULL REFERENCES pricing_provider(id),
          model_id TEXT NOT NULL REFERENCES canonical_model(id),
          source_kind TEXT NOT NULL CHECK(source_kind IN ('official','direct','relay','legacy-migration')),
          input_price REAL NOT NULL DEFAULT 0 CHECK(input_price >= 0),
          cache_price REAL NOT NULL DEFAULT 0 CHECK(cache_price >= 0),
          output_price REAL NOT NULL DEFAULT 0 CHECK(output_price >= 0),
          output_price_cny REAL,
          multiplier REAL NOT NULL DEFAULT 1.0 CHECK(multiplier > 0),
          currency TEXT NOT NULL,
          source_name TEXT,
          source_url TEXT,
          source_evidence TEXT,
          verified_at TEXT,
          legacy INTEGER NOT NULL DEFAULT 0 CHECK(legacy IN (0,1)),
          unpriced INTEGER NOT NULL DEFAULT 0 CHECK(unpriced IN (0,1)),
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
          CHECK(unpriced=1 OR input_price > 0 OR cache_price > 0 OR output_price > 0)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS provider_model_price_active
          ON provider_model_price(provider_id, model_id) WHERE active=1;
        CREATE TABLE IF NOT EXISTS relay_price_binding (
          id INTEGER PRIMARY KEY,
          relay_provider_id INTEGER NOT NULL REFERENCES pricing_provider(id),
          relay_model_id TEXT NOT NULL REFERENCES canonical_model(id),
          benchmark_provider_id INTEGER NOT NULL REFERENCES pricing_provider(id),
          benchmark_model_id TEXT NOT NULL REFERENCES canonical_model(id),
          source_name TEXT,
          source_url TEXT,
          source_evidence TEXT,
          active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS relay_price_binding_active
          ON relay_price_binding(relay_provider_id, relay_model_id) WHERE active=1;
        CREATE TABLE IF NOT EXISTS key_model_mapping (
          id INTEGER PRIMARY KEY,
          fingerprint TEXT NOT NULL REFERENCES source_provider(fingerprint),
          model_id TEXT NOT NULL REFERENCES canonical_model(id),
          target_provider_id INTEGER NOT NULL REFERENCES pricing_provider(id),
          target_model_id TEXT NOT NULL REFERENCES canonical_model(id),
          multiplier REAL NOT NULL DEFAULT 1.0 CHECK(multiplier > 0),
          enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          CHECK(model_id = lower(model_id)),
          CHECK(target_model_id = lower(target_model_id))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS key_model_mapping_active
          ON key_model_mapping(fingerprint, model_id) WHERE enabled=1;
        CREATE INDEX IF NOT EXISTS key_model_mapping_target
          ON key_model_mapping(target_provider_id, target_model_id);
        CREATE TABLE IF NOT EXISTS pricing_migration (
          version INTEGER PRIMARY KEY,
          status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
          started_at TEXT NOT NULL,
          completed_at TEXT,
          error TEXT
        );
        CREATE TABLE IF NOT EXISTS pricing_migration_conflict (
          id INTEGER PRIMARY KEY,
          migration_version INTEGER NOT NULL REFERENCES pricing_migration(version),
          fingerprint TEXT NOT NULL,
          legacy_multiplier REAL NOT NULL,
          applied_multiplier REAL NOT NULL,
          detail TEXT NOT NULL
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
          role TEXT, stage TEXT, currency TEXT, multiplier REAL, price REAL,
          price_source TEXT, price_comparable INTEGER, PRIMARY KEY(route_id, ordinal)
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
        self._upgrade_pricing_provider_schema()
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
            ('currency','TEXT'),
        ]:
            try: self.conn.execute(f'ALTER TABLE observation ADD COLUMN {name} {definition}')
            except sqlite3.OperationalError: pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS observation_created_at ON observation(created_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS observation_route_attempt ON observation(route_id, attempt_number)")
        try: self.conn.execute('ALTER TABLE source_provider ADD COLUMN request_headers BLOB')
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN api_key_mask TEXT NOT NULL DEFAULT '***'")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN site_id TEXT NOT NULL DEFAULT 'default'")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN pricing_provider_id INTEGER REFERENCES pricing_provider(id)")
        except sqlite3.OperationalError: pass
        try: self.conn.execute("ALTER TABLE source_provider ADD COLUMN pricing_provider_type TEXT")
        except sqlite3.OperationalError: pass
        for table in ('provider_model_price', 'relay_price_binding'):
            try: self.conn.execute(f'ALTER TABLE {table} ADD COLUMN source_name TEXT')
            except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE provider_model_price ADD COLUMN multiplier REAL NOT NULL DEFAULT 1.0')
        except sqlite3.OperationalError: pass
        try: self.conn.execute('ALTER TABLE provider_model_price ADD COLUMN output_price_cny REAL')
        except sqlite3.OperationalError: pass
        self.conn.execute("UPDATE provider_model_price SET output_price_cny=output_price WHERE output_price_cny IS NULL")
        for row in self.conn.execute("SELECT fingerprint,provider_type,base_url,models_json,pricing_provider_type FROM source_provider"):
            if row['pricing_provider_type']:
                continue
            try:
                models = json.loads(row['models_json'])
            except (TypeError, ValueError):
                models = []
            provider_type = canonical_provider_type(row['provider_type'], base_url=row['base_url'], models=models)
            if provider_type:
                self.conn.execute("UPDATE source_provider SET pricing_provider_type=? WHERE fingerprint=?", (provider_type, row['fingerprint']))
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
        for name, definition in [
            ("stage", "TEXT"), ("currency", "TEXT"), ("multiplier", "REAL"),
            ("price", "REAL"), ("price_source", "TEXT"), ("price_comparable", "INTEGER"),
        ]:
            try: self.conn.execute(f"ALTER TABLE route_candidate ADD COLUMN {name} {definition}")
            except sqlite3.OperationalError: pass
        self.conn.execute("CREATE INDEX IF NOT EXISTS route_run_telemetry_window ON route_run(telemetry_version, started_at DESC)")
        self.conn.execute("CREATE INDEX IF NOT EXISTS route_run_delivery_window ON route_run(delivery_mode, started_at DESC)")
        from .catalog import CATALOG, CATALOG_SEED_VERSION
        try: self.conn.execute("ALTER TABLE model_catalog ADD COLUMN currency TEXT NOT NULL DEFAULT 'USD'")
        except sqlite3.OperationalError: pass
        # The canonical model directory is metadata only. Fixed source prices
        # are seeded as ordinary Provider+Model rows; no model-rate projection
        # is created or consulted by the cutover.
        self.conn.executemany(
            "INSERT OR IGNORE INTO canonical_model(id,stage,family,active) VALUES(?,?,?,1)",
            [(model, item['intellect'], item['family']) for model, item in CATALOG.items()],
        )
        # The bundled catalog is only a migration seed.  Broker-owned active
        # models are the approved Stage list; later operator-created models
        # are left untouched by this cleanup.
        catalog_ids = tuple(CATALOG)
        approved_ids = tuple(APPROVED_MODEL_IDS)
        if catalog_ids:
            placeholders = ",".join("?" for _ in catalog_ids)
            approved_placeholders = ",".join("?" for _ in approved_ids)
            self.conn.execute(
                f"UPDATE canonical_model SET active=0 WHERE id IN ({placeholders}) AND id NOT IN ({approved_placeholders})",
                [*catalog_ids, *approved_ids],
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO pricing_provider(provider_key,name,provider_type,multiplier,active) VALUES('official-seed','Fixed official seed','official',1.0,1)"
        )
        # Retire the pre-cutover catalog Provider while preserving its rows for
        # historical inspection. It must not remain a usable pricing target.
        self.conn.execute("UPDATE pricing_provider SET active=0 WHERE provider_key='official-catalog'")
        official_seed_id = self.conn.execute(
            "SELECT id FROM pricing_provider WHERE provider_key='official-seed'"
        ).fetchone()[0]
        for model, item in CATALOG.items():
            self.conn.execute(
                """INSERT OR IGNORE INTO provider_model_price(
                    provider_id,model_id,source_kind,input_price,cache_price,output_price,output_price_cny,currency,
                    source_name,source_url,source_evidence,verified_at,legacy,unpriced,active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (
                    official_seed_id, model, 'official', item['official_output_price_cny'],
                    item['official_output_price_cny'], item['official_output_price_cny'], item['official_output_price_cny'], 'CNY',
                    OFFICIAL_PRICE_SNAPSHOT['source_name'], OFFICIAL_PRICE_SNAPSHOT['source_url'],
                    OFFICIAL_PRICE_SNAPSHOT['source_evidence'], OFFICIAL_PRICE_SNAPSHOT['verified_at'], 0,
                    int(item['official_output_price_cny'] <= 0),
                ),
            )
        # Keep official vendor identities separate from the fixed seed. Every
        # row remains an explicit Provider+Model price, including relay rows.
        for provider_key, snapshot in OFFICIAL_PROVIDER_SNAPSHOTS.items():
            self.conn.execute(
                "INSERT OR IGNORE INTO pricing_provider(provider_key,name,provider_type,multiplier,active) VALUES(?,?,?,?,1)",
                (provider_key, snapshot["name"], "official", 1.0),
            )
            provider_id = self.conn.execute(
                "SELECT id FROM pricing_provider WHERE provider_key=?", (provider_key,)
            ).fetchone()[0]
            for model in sorted(snapshot["models"]):
                item = CATALOG[model]
                self.conn.execute(
                    "INSERT OR IGNORE INTO canonical_model(id,stage,family,active) VALUES(?,?,?,1)",
                    (model, item["intellect"], item["family"]),
                )
                self.conn.execute(
                    """INSERT OR IGNORE INTO provider_model_price(
                       provider_id,model_id,source_kind,input_price,cache_price,output_price,output_price_cny,
                       multiplier,currency,source_name,source_url,source_evidence,verified_at,
                       legacy,unpriced,active
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    (
                        provider_id, model, "official", item["official_output_price_cny"],
                        item["official_output_price_cny"], item["official_output_price_cny"], item["official_output_price_cny"], 1.0,
                        "CNY", OFFICIAL_PRICE_SNAPSHOT["source_name"],
                        snapshot["source_url"], OFFICIAL_PRICE_SNAPSHOT["source_evidence"],
                        OFFICIAL_PRICE_SNAPSHOT["verified_at"], 0,
                        int(item["official_output_price_cny"] <= 0),
                    ),
                )
        self.conn.execute(
            "INSERT INTO broker_setting(name,value) VALUES('pricing_mapping_version',?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value",
            (str(PRICING_MIGRATION_VERSION),),
        )
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
        if not self.conn.execute(
            "SELECT 1 FROM pricing_migration WHERE version=? AND status='completed'",
            (PRICING_MIGRATION_VERSION,),
        ).fetchone():
            self.migrate_pricing()
        self.conn.commit()

    def canonical_models(self, *, active: bool | None = True) -> dict[str, dict]:
        clauses = []
        params: list[object] = []
        if active is not None:
            clauses.append("active=?")
            params.append(int(active))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            "SELECT id,stage,family,active FROM canonical_model" + where + " ORDER BY id", params
        ).fetchall()
        return {
            row['id']: {
                'id': row['id'], 'stage': row['stage'], 'family': row['family'],
                'active': bool(row['active']),
            }
            for row in rows
        }

    def stage_models(self, stage: str) -> tuple[str, ...]:
        """Return the active Broker-owned models currently assigned to a Stage."""
        rows = self.conn.execute(
            "SELECT id FROM canonical_model WHERE stage=? AND active=1", (stage,)
        ).fetchall()
        available = {row["id"] for row in rows}
        approved = tuple(model for model in APPROVED_STAGE_MODELS.get(stage, ()) if model in available)
        custom = tuple(sorted(available - set(approved)))
        return approved + custom

    def create_canonical_model(self, model_id: str, *, stage: str, family: str) -> bool:
        if not model_id or stage not in {'standard', 'smart', 'expert'} or not family:
            raise ValueError('canonical model requires id, stage, and family')
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO canonical_model(id,stage,family,active) VALUES(?,?,?,1)",
                    (model_id, stage, family),
                )
                self.record_configuration_change(
                    f'model:{model_id}', None,
                    {'stage': stage, 'family': family, 'active': True}, source='pricing',
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def update_canonical_model(self, model_id: str, *, stage: str, family: str,
                               active: bool | None = None) -> bool:
        if not model_id or stage not in {'standard', 'smart', 'expert'} or not isinstance(family, str) or not family.strip():
            raise ValueError('canonical model requires id, stage, and family')
        row = self.conn.execute(
            "SELECT stage,family,active FROM canonical_model WHERE id=?", (model_id,)
        ).fetchone()
        if row is None:
            return False
        next_active = bool(row['active']) if active is None else active
        before = {'stage': row['stage'], 'family': row['family'], 'active': bool(row['active'])}
        after = {'stage': stage, 'family': family, 'active': next_active}
        with self.conn:
            self.conn.execute(
                "UPDATE canonical_model SET stage=?,family=?,active=? WHERE id=?",
                (stage, family, int(next_active), model_id),
            )
            self.record_configuration_change(f'model:{model_id}', before, after, source='pricing')
        return True

    def deactivate_canonical_model(self, model_id: str) -> bool:
        row = self.conn.execute("SELECT stage,family,active FROM canonical_model WHERE id=?", (model_id,)).fetchone()
        if row is None:
            return False
        with self.conn:
            changed = bool(self.conn.execute(
                "UPDATE canonical_model SET active=0 WHERE id=?", (model_id,)
            ).rowcount)
            if changed:
                self.record_configuration_change(
                    f'model:{model_id}', dict(row) | {'active': bool(row['active'])},
                    {'stage': row['stage'], 'family': row['family'], 'active': False}, source='pricing',
                )
            return changed

    def delete_canonical_model(self, model_id: str) -> bool:
        referenced = self.conn.execute(
            """SELECT 1 FROM provider_model_price WHERE model_id=?
               UNION ALL SELECT 1 FROM key_model_mapping WHERE model_id=? OR target_model_id=?
               LIMIT 1""", (model_id, model_id, model_id)
        ).fetchone()
        if referenced:
            raise ValueError('canonical model is referenced; deactivate it instead')
        with self.conn:
            return bool(self.conn.execute("DELETE FROM canonical_model WHERE id=?", (model_id,)).rowcount)

    def create_pricing_provider(self, provider_key: str, *, provider_type: str,
                                name: str | None = None, multiplier: float | None = None) -> int:
        provider_type = require_provider_type(provider_type)
        if not provider_key:
            raise ValueError('pricing provider key is required')
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO pricing_provider(provider_key,name,provider_type,multiplier,active)
                   VALUES(?,?,?,?,1)""",
                (provider_key, name or provider_key, provider_type, 1.0),
            )
            self.record_configuration_change(
                f'pricing-provider:{cursor.lastrowid}', None,
                {'provider_key': provider_key, 'name': name or provider_key,
                 'provider_type': provider_type, 'active': True}, source='pricing',
            )
        return int(cursor.lastrowid)

    def pricing_providers(self, *, active: bool | None = None) -> list[dict]:
        placeholders = ','.join('?' for _ in PROVIDER_TYPES)
        clause = f' WHERE provider_type IN ({placeholders})' + ('' if active is None else ' AND active=?')
        params = tuple(sorted(PROVIDER_TYPES)) if active is None else tuple(sorted(PROVIDER_TYPES)) + (int(active),)
        rows = self.conn.execute(
            "SELECT id,provider_key,name,provider_type,active FROM pricing_provider" + clause + " ORDER BY id",
            params,
        ).fetchall()
        return [dict(row) | {'active': bool(row['active'])} for row in rows]

    def update_pricing_provider(self, provider_id: int, *, name: str, provider_type: str,
                                multiplier: float | None = None, active: bool | None = None) -> bool:
        provider_type = require_provider_type(provider_type)
        if not isinstance(name, str) or not name.strip():
            raise ValueError('pricing provider requires name')
        row = self.conn.execute(
            "SELECT provider_key,name,provider_type,active FROM pricing_provider WHERE id=?",
            (provider_id,),
        ).fetchone()
        if row is None:
            return False
        next_active = bool(row['active']) if active is None else active
        before = dict(row) | {'active': bool(row['active'])}
        after = {'provider_key': row['provider_key'], 'name': name, 'provider_type': provider_type,
                 'active': next_active}
        with self.conn:
            self.conn.execute(
                "UPDATE pricing_provider SET name=?,provider_type=?,active=? WHERE id=?",
                (name, provider_type, int(next_active), provider_id),
            )
            self.record_configuration_change(f'pricing-provider:{provider_id}', before, after, source='pricing')
        return True

    def deactivate_pricing_provider(self, provider_id: int) -> bool:
        row = self.conn.execute("SELECT * FROM pricing_provider WHERE id=?", (provider_id,)).fetchone()
        if row is None:
            return False
        with self.conn:
            changed = bool(self.conn.execute(
                "UPDATE pricing_provider SET active=0 WHERE id=?", (provider_id,)
            ).rowcount)
            if changed:
                self.record_configuration_change(
                    f'pricing-provider:{provider_id}', dict(row) | {'active': bool(row['active'])},
                    dict(row) | {'active': False}, source='pricing',
                )
            return changed

    def delete_pricing_provider(self, provider_id: int) -> bool:
        referenced = self.conn.execute(
            """SELECT 1 FROM provider_model_price WHERE provider_id=?
               UNION ALL SELECT 1 FROM key_model_mapping WHERE target_provider_id=? LIMIT 1""",
            (provider_id, provider_id),
        ).fetchone()
        if referenced:
            raise ValueError('pricing provider is referenced; deactivate it instead')
        with self.conn:
            return bool(self.conn.execute("DELETE FROM pricing_provider WHERE id=?", (provider_id,)).rowcount)

    def insert_provider_model_price(self, *, provider_id: int, model_id: str,
                                    output_price_cny: float | None = None,
                                    source_kind: str | None = None, input_price: float | None = None,
                                    cache_price: float | None = None, output_price: float | None = None,
                                    currency: str | None = None, multiplier: float | None = None,
                                    source_name: str | None = None,
                                    source_url: str | None = None,
                                    source_evidence: str | None = None,
                                    verified_at: str | None = None,
                                    legacy: bool = False, unpriced: bool | None = None) -> int:
        del source_kind, currency, multiplier
        if output_price_cny is None:
            output_price_cny = output_price
        if not isinstance(output_price_cny, (int, float)) or not math.isfinite(output_price_cny) or output_price_cny < 0:
            raise ValueError('CNY output price must be non-negative')
        output_price_cny = float(output_price_cny)
        # The old component arguments are accepted only as a migration shim.
        input_price = cache_price = output_price = output_price_cny
        source_kind = 'direct'
        currency = 'CNY'
        provider_row = self.conn.execute(
            "SELECT active FROM pricing_provider WHERE id=?", (provider_id,)
        ).fetchone()
        # Only key_model_mapping owns a multiplier. The physical legacy
        # columns are retained solely so old databases can be opened; new
        # writes always store neutral/false values there.
        multiplier = 1.0
        legacy = False
        if unpriced is None:
            unpriced = output_price_cny <= 0
        if not unpriced and output_price_cny <= 0:
            raise ValueError('a priced row needs a non-zero price')
        model_id = canonicalize(model_id)
        model = self.conn.execute("SELECT active FROM canonical_model WHERE id=?", (model_id,)).fetchone()
        if model is None:
            self.conn.execute(
                "INSERT OR IGNORE INTO canonical_model(id,stage,family,active) VALUES(?,?,?,0)",
                (model_id, 'standard', 'dynamic'),
            )
            model = self.conn.execute("SELECT active FROM canonical_model WHERE id=?", (model_id,)).fetchone()
        if not provider_row or not provider_row['active'] or not model:
            raise sqlite3.IntegrityError('price requires active provider and model')
        transaction = nullcontext() if self.conn.in_transaction else self.conn
        with transaction:
            cursor = self.conn.execute(
                """INSERT INTO provider_model_price(
                   provider_id,model_id,source_kind,input_price,cache_price,output_price,output_price_cny,multiplier,currency,
                   source_name,source_url,source_evidence,verified_at,legacy,unpriced,active
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (provider_id, model_id, source_kind, input_price, cache_price, output_price, output_price_cny,
                 1.0, currency, source_name, source_url, source_evidence, verified_at, int(legacy), int(unpriced)),
            )
            self.record_configuration_change(
                f"pricing:{provider_id}:{model_id}", None,
                {'output_price_cny': output_price_cny, 'source_name': source_name,
                 'source_url': source_url, 'source_evidence': source_evidence, 'verified_at': verified_at,
                 'unpriced': bool(unpriced)}, source='pricing',
            )
        return int(cursor.lastrowid)

    def upsert_provider_model_price(self, **kwargs) -> int:
        if kwargs.get('output_price_cny') is None:
            kwargs['output_price_cny'] = kwargs.get('output_price')
        value = kwargs['output_price_cny']
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError('CNY output price must be non-negative')
        kwargs['output_price_cny'] = float(value)
        kwargs['input_price'] = kwargs['cache_price'] = kwargs['output_price'] = float(value)
        kwargs['currency'] = 'CNY'
        existing = self.conn.execute(
            "SELECT id FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1",
            (kwargs['provider_id'], kwargs['model_id']),
        ).fetchone()
        if existing:
            before = self.conn.execute(
                "SELECT output_price_cny,source_name,source_url,source_evidence,verified_at,unpriced FROM provider_model_price WHERE id=?",
                (existing['id'],),
            ).fetchone()
            values = kwargs.copy()
            values.pop('provider_id'); values.pop('model_id')
            values['multiplier'] = 1.0
            values['legacy'] = False
            values.setdefault('unpriced', None)
            values['unpriced'] = int(values['unpriced'] if values['unpriced'] is not None else values['output_price_cny'] <= 0)
            transaction = nullcontext() if self.conn.in_transaction else self.conn
            with transaction:
                self.conn.execute(
                    """UPDATE provider_model_price SET source_kind='direct',input_price=?,cache_price=?,output_price=?,
                       output_price_cny=?,multiplier=1.0,currency='CNY',source_name=?,source_url=?,source_evidence=?,verified_at=?,legacy=0,unpriced=? WHERE id=?""",
                    (values['output_price_cny'], values['output_price_cny'], values['output_price_cny'], values['output_price_cny'],
                     values.get('source_name'), values.get('source_url'), values.get('source_evidence'), values.get('verified_at'),
                     int(values.get('unpriced', values['output_price_cny'] <= 0)), existing['id']),
                )
            after = {
                'output_price_cny': values['output_price_cny'], 'source_name': values.get('source_name'), 'source_url': values.get('source_url'),
                'source_evidence': values.get('source_evidence'), 'verified_at': values.get('verified_at'),
                'unpriced': bool(values['unpriced']),
            }
            self.record_configuration_change(
                f"pricing:{kwargs['provider_id']}:{kwargs['model_id']}",
                dict(before), after, source='pricing',
            )
            return int(existing['id'])
        return self.insert_provider_model_price(**kwargs)

    def deactivate_provider_model_price(self, provider_id: int, model_id: str) -> bool:
        row = self.conn.execute(
            "SELECT * FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1",
            (provider_id, model_id),
        ).fetchone()
        if row is None:
            return False
        with self.conn:
            changed = bool(self.conn.execute(
                "UPDATE provider_model_price SET active=0 WHERE provider_id=? AND model_id=? AND active=1",
                (provider_id, model_id),
            ).rowcount)
            if changed:
                self.record_configuration_change(
                    f"pricing:{provider_id}:{model_id}", dict(row),
                    dict(row) | {'active': 0}, source='pricing',
                )
            return changed

    def provider_model_prices(self, *, active: bool | None = None) -> list[dict]:
        placeholders = ','.join('?' for _ in PROVIDER_TYPES)
        clause = f' WHERE pp.provider_type IN ({placeholders}) AND p.legacy=0' + ('' if active is None else ' AND p.active=?')
        params = tuple(sorted(PROVIDER_TYPES)) if active is None else tuple(sorted(PROVIDER_TYPES)) + (int(active),)
        rows = self.conn.execute(
            """SELECT p.id,p.provider_id,p.model_id,p.source_kind,p.input_price,p.cache_price,p.output_price,
                      p.output_price_cny,p.currency,p.source_name,p.source_url,p.source_evidence,p.verified_at,p.unpriced,p.active,
                      pp.provider_key,pp.name provider_name,pp.provider_type
               FROM provider_model_price p JOIN pricing_provider pp ON pp.id=p.provider_id""" + clause +
            " ORDER BY p.id", params
        ).fetchall()
        return [dict(row) | {'active': bool(row['active']), 'unpriced': bool(row['unpriced'])}
                for row in rows]

    def _validate_key_model_mapping_target(self, fingerprint: str, model_id: str,
                                            target_provider_id: int, target_model_id: str) -> tuple[str, str]:
        model_id = canonicalize(model_id)
        target_model_id = canonicalize(target_model_id)
        source = self.conn.execute(
            "SELECT 1 FROM source_provider WHERE fingerprint=?", (fingerprint,)
        ).fetchone()
        model = self.conn.execute(
            "SELECT 1 FROM canonical_model WHERE id=?", (model_id,)
        ).fetchone()
        target_model = self.conn.execute(
            "SELECT 1 FROM canonical_model WHERE id=?", (target_model_id,)
        ).fetchone()
        provider = self.conn.execute(
            "SELECT 1 FROM pricing_provider WHERE id=? AND active=1 AND provider_type IN (?,?,?,?,?,?)",
            (target_provider_id, *sorted(PROVIDER_TYPES)),
        ).fetchone()
        price = self.conn.execute(
            "SELECT 1 FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1 AND source_kind <> 'legacy-migration' AND legacy=0",
            (target_provider_id, target_model_id),
        ).fetchone()
        if not source:
            raise ValueError("key fingerprint is not configured")
        if not model or not target_model:
            raise ValueError("mapping requires known canonical models")
        if not provider:
            raise ValueError("mapping requires an active target provider")
        if not price:
            raise ValueError("mapping requires an active Provider+Model price row")
        return model_id, target_model_id

    def create_key_model_mapping(self, fingerprint: str, model_id: str,
                                 target_provider_id: int, target_model_id: str | None = None,
                                 multiplier: float = 1.0, enabled: bool = True) -> int:
        if not fingerprint or not math.isfinite(multiplier) or multiplier <= 0:
            raise ValueError("key mapping requires a positive multiplier")
        target_model_id = target_model_id or model_id
        model_id, target_model_id = self._validate_key_model_mapping_target(
            fingerprint, model_id, target_provider_id, target_model_id,
        )
        stamp = self._timestamp()
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO key_model_mapping(
                   fingerprint,model_id,target_provider_id,target_model_id,multiplier,enabled,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?)""",
                (fingerprint, model_id, target_provider_id, target_model_id, multiplier, int(enabled), stamp, stamp),
            )
            self.record_configuration_change(
                f"key-mapping:{cursor.lastrowid}", None,
                {"fingerprint": fingerprint, "model_id": model_id,
                 "target_provider_id": target_provider_id, "target_model_id": target_model_id,
                 "multiplier": multiplier, "enabled": bool(enabled)}, source="pricing",
            )
        return int(cursor.lastrowid)

    def upsert_key_model_mapping(self, fingerprint: str, model_id: str,
                                 target_provider_id: int, target_model_id: str | None = None,
                                 multiplier: float = 1.0, enabled: bool = True) -> int:
        target_model_id = target_model_id or model_id
        model_id, target_model_id = self._validate_key_model_mapping_target(
            fingerprint, model_id, target_provider_id, target_model_id,
        )
        existing = self.conn.execute(
            "SELECT id FROM key_model_mapping WHERE fingerprint=? AND model_id=? ORDER BY enabled DESC,id DESC LIMIT 1",
            (fingerprint, model_id),
        ).fetchone()
        if existing:
            self.update_key_model_mapping(
                existing["id"], target_provider_id=target_provider_id,
                target_model_id=target_model_id, multiplier=multiplier, enabled=enabled,
            )
            return int(existing["id"])
        return self.create_key_model_mapping(
            fingerprint, model_id, target_provider_id=target_provider_id,
            target_model_id=target_model_id, multiplier=multiplier, enabled=enabled,
        )

    def update_key_model_mapping(self, mapping_id: int, *, target_provider_id: int | None = None,
                                 target_model_id: str | None = None, multiplier: float | None = None,
                                 enabled: bool | None = None) -> bool:
        current = self.conn.execute("SELECT * FROM key_model_mapping WHERE id=?", (mapping_id,)).fetchone()
        if current is None:
            return False
        provider_id = current["target_provider_id"] if target_provider_id is None else target_provider_id
        model_id, target_model = self._validate_key_model_mapping_target(
            current["fingerprint"], current["model_id"], provider_id,
            target_model_id or current["target_model_id"],
        )
        next_multiplier = current["multiplier"] if multiplier is None else multiplier
        if not math.isfinite(next_multiplier) or next_multiplier <= 0:
            raise ValueError("key mapping requires a positive multiplier")
        next_enabled = bool(current["enabled"]) if enabled is None else bool(enabled)
        with self.conn:
            self.conn.execute(
                """UPDATE key_model_mapping SET target_provider_id=?,target_model_id=?,multiplier=?,enabled=?,updated_at=?
                   WHERE id=?""",
                (provider_id, target_model, next_multiplier, int(next_enabled), self._timestamp(), mapping_id),
            )
            self.record_configuration_change(
                f"key-mapping:{mapping_id}", dict(current),
                dict(current) | {"target_provider_id": provider_id, "target_model_id": target_model,
                                 "multiplier": next_multiplier, "enabled": int(next_enabled)}, source="pricing",
            )
        return True

    def deactivate_key_model_mapping(self, fingerprint: str, model_id: str) -> bool:
        model_id = canonicalize(model_id)
        row = self.conn.execute(
            "SELECT * FROM key_model_mapping WHERE fingerprint=? AND model_id=? AND enabled=1",
            (fingerprint, model_id),
        ).fetchone()
        if row is None:
            return False
        with self.conn:
            changed = bool(self.conn.execute(
                "UPDATE key_model_mapping SET enabled=0,updated_at=? WHERE id=?", (self._timestamp(), row["id"]),
            ).rowcount)
            if changed:
                self.record_configuration_change(
                    f"key-mapping:{row['id']}", dict(row), dict(row) | {"enabled": 0}, source="pricing",
                )
            return changed

    def key_model_mappings(self, *, fingerprint: str | None = None, model: str | None = None,
                           enabled: bool | None = None) -> list[dict]:
        clauses, params = [], []
        if fingerprint is not None:
            clauses.append("m.fingerprint=?"); params.append(fingerprint)
        if model is not None:
            clauses.append("m.model_id=?"); params.append(canonicalize(model))
        if enabled is not None:
            clauses.append("m.enabled=?"); params.append(int(enabled))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.conn.execute(
            """SELECT m.*,tp.provider_key target_provider_key,tp.name target_provider_name,
                      tp.provider_type target_provider_type
                 FROM key_model_mapping m JOIN pricing_provider tp ON tp.id=m.target_provider_id"""
            + where + " ORDER BY m.fingerprint,m.model_id,m.id", params,
        ).fetchall()
        return [
            dict(row) | {
                "enabled": bool(row["enabled"]), "active": bool(row["enabled"]),
                "provider_id": row["target_provider_id"], "price_provider_id": row["target_provider_id"],
                "price_model_id": row["target_model_id"],
            }
            for row in rows
        ]

    def pricing_entries(self, *, provider: str | None = None, model: str | None = None,
                        stage: str | None = None, currency: str | None = None,
                        include_inactive: bool = False, status: str | None = None) -> list[dict]:
        providers = self.pricing_providers(active=None if include_inactive else True)
        provider_ids = {item['id'] for item in providers}
        if provider:
            provider_ids = {item['id'] for item in providers if provider in {str(item['id']), item['provider_key'], item['name']}}
        raw_prices = self.provider_model_prices(active=None if include_inactive else True)
        model_rows = self.conn.execute("SELECT id,stage,family,active FROM canonical_model").fetchall()
        model_map = {
            row['id']: {'id': row['id'], 'stage': row['stage'], 'family': row['family'], 'active': bool(row['active'])}
            for row in model_rows
            if include_inactive or bool(row['active'])
        }
        if model:
            model_id = canonicalize(model)
            model_map = {key: value for key, value in model_map.items() if key == model_id}
        by_key = {(row['provider_id'], row['model_id']): row for row in raw_prices if row['provider_id'] in provider_ids and row['model_id'] in model_map}
        candidates = set(by_key)
        for row in self.conn.execute('SELECT pricing_provider_id,models_json FROM source_provider WHERE pricing_provider_id IS NOT NULL').fetchall():
            if row['pricing_provider_id'] not in provider_ids:
                continue
            try:
                inventory_models = json.loads(row['models_json'])
            except (TypeError, ValueError):
                inventory_models = []
            candidates.update((row['pricing_provider_id'], canonicalize(item)) for item in inventory_models if canonicalize(item) in model_map and row['pricing_provider_id'] in provider_ids)
        provider_map = {item['id']: item for item in providers}
        inventory_map = {}
        for row in self.conn.execute('SELECT pricing_provider_id,models_json FROM source_provider WHERE pricing_provider_id IS NOT NULL').fetchall():
            try:
                listed = json.loads(row['models_json'])
            except (TypeError, ValueError):
                listed = []
            inventory_map.setdefault(row['pricing_provider_id'], set()).update(listed)
        result = []
        for provider_id, model_id in sorted(candidates, key=lambda item: (item[0], item[1])):
            price = by_key.get((provider_id, model_id))
            provider_item = provider_map[provider_id]
            model_item = model_map[model_id]
            resolved = self.effective_pricing(provider_id, model_id)
            display_price = price
            if currency and currency.upper() != 'CNY':
                continue
            active = bool(price['active']) if price else bool(provider_item['active'] and model_item['active'])
            priced = bool(resolved['priced'])
            row_status = 'priced' if priced else 'unpriced'
            if status and status != row_status:
                continue
            source = {
                'kind': 'pricing' if price else None,
                'type': 'pricing' if price else None,
                'name': price['source_name'] if price else None,
                'url': price['source_url'] if price else None,
                'evidence': price['source_evidence'] if price else None,
                'verified_at': price['verified_at'] if price else None,
            }
            base = ({'input': price['output_price_cny'], 'cache': price['output_price_cny'], 'output': price['output_price_cny'], 'currency': 'CNY'}
                    if price else {'input': None, 'cache': None, 'output': None, 'currency': None})
            final = ({'input': resolved['input_price'], 'cache': resolved['cache_price'], 'output': resolved['output_price'],
                      'blended': resolved['blended_price'], 'currency': resolved['currency']}
                     if resolved['priced'] else None)
            result.append({
                'id': price['id'] if price else None, 'active': active, 'unpriced': not priced,
                'status': row_status, 'provider': provider_item | {'type': provider_item['provider_type'],
                    'inventory_models': sorted(inventory_map.get(provider_id, set()))},
                'model': {key: model_item[key] for key in ('id', 'stage', 'family', 'active')},
                'base_price': base, 'final_price': final, 'source': source,
                'reason': resolved['reason'],
            })
        return result

    def effective_pricing(self, provider: int | str, model: str) -> dict:
        """Resolve final prices for one pricing Provider and canonical Model.

        The returned component prices already include the Provider+Model row's
        multiplier. A missing or explicitly unpriced row is represented as an
        unknown result, never as a zero-cost result.
        """
        model_id = canonicalize(model)
        if isinstance(provider, int):
            provider_row = self.conn.execute(
                "SELECT * FROM pricing_provider WHERE id=? AND provider_type IN (?,?,?,?,?,?)",
                (provider, *sorted(PROVIDER_TYPES)),
            ).fetchone()
        else:
            provider_row = self.conn.execute(
                "SELECT * FROM pricing_provider WHERE provider_key=? AND provider_type IN (?,?,?,?,?,?)",
                (provider, *sorted(PROVIDER_TYPES)),
            ).fetchone()

        base = {"model": model_id, "stage": None, "currency": "CNY",
                "input_price": None, "cache_price": None, "output_price": None,
                "output_price_cny": None, "blended_price": None, "source": None,
                "priced": False, "status": "UNKNOWN", "reason": None}
        model_row = self.conn.execute(
            "SELECT stage FROM canonical_model WHERE id=?", (model_id,)
        ).fetchone()
        if model_row:
            base["stage"] = model_row["stage"]
        if provider_row is None or not provider_row["active"]:
            base["reason"] = "pricing provider is missing or inactive"
            return base
        price_provider_id = provider_row["id"]
        price_model_id = model_id
        price = self.conn.execute(
            """SELECT output_price_cny,output_price,source_kind,unpriced
               FROM provider_model_price
               WHERE provider_id=? AND model_id=? AND active=1 AND source_kind <> 'legacy-migration' AND legacy=0""",
            (price_provider_id, price_model_id),
        ).fetchone()
        if price is None:
            base["reason"] = "provider model price is missing"
            return base
        if price["unpriced"]:
            base["reason"] = "provider model price is explicitly unpriced"
            return base

        # Price rows have one CNY output rate. Keep component aliases in the
        # internal response for older callers, but derive every one from it.
        output_price = float(price["output_price_cny"] if price["output_price_cny"] is not None else price["output_price"])
        components = [round(output_price, 10)] * 3
        base.update({
            "currency": "CNY", "input_price": components[0],
            "cache_price": components[1], "output_price": components[2],
            "output_price_cny": output_price,
            "blended_price": output_price,
            "source": "pricing", "priced": True, "status": "priced",
        })
        return base

    def effective_key_pricing(self, fingerprint: str, model: str) -> dict:
        """Resolve pricing for one configured API key and model."""
        model_id = canonicalize(model)
        mapping = self.conn.execute(
            """SELECT id,target_provider_id,target_model_id,multiplier,enabled
               FROM key_model_mapping
               WHERE fingerprint=? AND model_id=? AND enabled=1""",
            (fingerprint, model_id),
        ).fetchone()
        if mapping is None:
            model_row = self.conn.execute(
                "SELECT stage FROM canonical_model WHERE id=? AND active=1", (model_id,)
            ).fetchone()
            return {
                "model": model_id, "stage": model_row["stage"] if model_row else None, "currency": "CNY",
                "input_price": None, "cache_price": None, "output_price": None,
                "output_price_cny": None, "blended_price": None, "multiplier": None, "source": None,
                "priced": False, "status": "UNKNOWN", "reason": "key-model mapping is missing or disabled",
            }
        resolved = self.effective_pricing(mapping["target_provider_id"], mapping["target_model_id"])
        if not resolved["priced"]:
            return resolved | {"model": model_id, "mapping_id": mapping["id"], "mapping_multiplier": mapping["multiplier"],
                               "reason": resolved["reason"] or "mapped Provider+Model price is unpriced"}
        multiplier = float(mapping["multiplier"])
        components = [round(float(resolved["output_price_cny"]) * multiplier, 10)] * 3
        return resolved | {
            "model": model_id,
            "input_price": components[0], "cache_price": components[1], "output_price": components[2],
            "output_price_cny": components[2], "blended_price": components[2], "status": "priced",
            "multiplier": multiplier, "mapping_id": mapping["id"],
            "mapping_multiplier": multiplier, "mapped_provider_id": mapping["target_provider_id"],
            "mapped_model": mapping["target_model_id"],
        }

    def bind_relay_price(self, relay_provider_id: int, relay_model_id: str,
                         benchmark_provider_id: int, benchmark_model_id: str,
                         *, source_name: str | None = None, source_url: str | None = None,
                         source_evidence: str | None = None) -> int:
        raise ValueError('relay price bindings have been removed; create an explicit relay Provider+Model price')

    def update_relay_price_binding(self, binding_id: int, *, benchmark_provider_id: int,
                                   benchmark_model_id: str, source_name: str | None = None,
                                   source_url: str | None = None,
                                   source_evidence: str | None = None) -> bool:
        raise ValueError('relay price bindings have been removed; update the explicit relay Provider+Model price')

    def relay_price_bindings(self, *, active: bool | None = None) -> list[dict]:
        return []

    def deactivate_relay_price_binding(self, relay_provider_id: int, relay_model_id: str) -> bool:
        return False

    def migrate_pricing(self) -> dict:
        version = PRICING_MIGRATION_VERSION
        complete = self.conn.execute(
            "SELECT 1 FROM pricing_migration WHERE version=? AND status='completed'", (version,)
        ).fetchone()
        needs_mapping = False
        if complete:
            for source in self.conn.execute("SELECT fingerprint,models_json FROM source_provider"):
                try:
                    source_models = json.loads(source["models_json"])
                except (TypeError, ValueError):
                    source_models = []
                for raw_model in source_models:
                    model_id = canonicalize(str(raw_model))
                    if model_id != "unavailable" and self.conn.execute(
                        "SELECT 1 FROM canonical_model WHERE id=? AND active=1", (model_id,)
                    ).fetchone() and not self.conn.execute(
                        "SELECT 1 FROM key_model_mapping WHERE fingerprint=? AND model_id=?",
                        (source["fingerprint"], model_id),
                    ).fetchone():
                        needs_mapping = True
                        break
                if needs_mapping:
                    break
        if complete and not needs_mapping:
            return {'version': version, 'migrated': False, 'conflicts': 0}
        started = self._timestamp()
        conflicts = 0
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO pricing_migration(version,status,started_at,completed_at,error) VALUES(?,?,?,?,NULL)",
                    (version, 'running', started, None),
                )
                provider_rows = self.conn.execute(
                    "SELECT s.* FROM source_provider s JOIN policy p USING(fingerprint)"
                ).fetchall()
                for row in provider_rows:
                    source = json.loads(row['source_json']) if row['source_json'] else {}
                    provider_type = canonical_provider_type(
                        row['pricing_provider_type'] or source.get('provider_type') or row['provider_type'],
                        base_url=row['base_url'], models=json.loads(row['models_json']),
                    )
                    if provider_type is None:
                        continue
                    key = provider_type
                    provider_row = self.conn.execute(
                        "SELECT id FROM pricing_provider WHERE provider_key=? AND provider_type=?",
                        (key, provider_type),
                    ).fetchone()
                    if provider_row is None:
                        provider_row = self.conn.execute(
                            "SELECT id FROM pricing_provider WHERE provider_type=? AND active=1 ORDER BY id LIMIT 1",
                            (provider_type,),
                        ).fetchone()
                    if provider_row is None:
                        self.conn.execute(
                            "INSERT INTO pricing_provider(provider_key,name,provider_type,multiplier,active) VALUES(?,?,?,?,1)",
                            (key, row['name'], provider_type, 1.0),
                        )
                        provider_row = self.conn.execute("SELECT id FROM pricing_provider WHERE provider_key=?", (key,)).fetchone()
                    provider_id = provider_row['id']
                    self.conn.execute("UPDATE pricing_provider SET active=1 WHERE id=?", (provider_id,))
                    self.conn.execute(
                        "UPDATE source_provider SET pricing_provider_id=?,pricing_provider_type=? WHERE fingerprint=?",
                        (provider_id, provider_type, row['fingerprint']),
                    )
                    for raw_model in json.loads(row['models_json']):
                        model_id = canonicalize(str(raw_model))
                        if model_id == 'unavailable':
                            continue
                        target_id = provider_id
                        target_model = model_id
                        existing_price = self.conn.execute(
                            "SELECT 1 FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1",
                            (provider_id, model_id),
                        ).fetchone()
                        if not existing_price:
                            legacy_price = self.conn.execute(
                                """SELECT p.output_price_cny,p.output_price,p.unpriced
                                   FROM provider_model_price p JOIN pricing_provider old ON old.id=p.provider_id
                                   WHERE old.provider_key LIKE 'key-source:%' AND p.model_id=? AND p.active=1
                                   ORDER BY p.id DESC LIMIT 1""",
                                (model_id,),
                            ).fetchone()
                            preserved_output = legacy_price['output_price_cny'] if legacy_price and legacy_price['output_price_cny'] is not None else legacy_price['output_price'] if legacy_price else DEFAULT_OUTPUT_PRICE_CNY
                            self.insert_provider_model_price(
                                provider_id=provider_id, model_id=model_id, source_kind=provider_type,
                                output_price_cny=preserved_output,
                                source_name='pricing-domain-migration',
                                source_evidence='Preserved existing Provider+Model price during CPA identity migration' if legacy_price else 'Default CNY output price for a migrated CPA combination',
                                unpriced=bool(legacy_price['unpriced']) if legacy_price else False,
                            )
                        current_mapping = self.conn.execute(
                            "SELECT target_provider_id,multiplier FROM key_model_mapping WHERE fingerprint=? AND model_id=? AND enabled=1",
                            (row['fingerprint'], model_id),
                        ).fetchone()
                        mapping_multiplier = float(current_mapping['multiplier']) if current_mapping else 1.0
                        self.upsert_key_model_mapping(
                            row['fingerprint'], model_id, target_provider_id=target_id,
                            target_model_id=target_model, multiplier=mapping_multiplier, enabled=True,
                        )
                self.conn.execute(
                    "UPDATE pricing_migration SET status='completed',completed_at=?,error=NULL WHERE version=?",
                    (self._timestamp(), version),
                )
                self.conn.execute(
                    "INSERT INTO broker_setting(name,value) VALUES('pricing_domain_seed_version',?) "
                    "ON CONFLICT(name) DO UPDATE SET value=excluded.value", (str(version),)
                )
        except Exception as exc:
            with self.conn:
                self.conn.execute(
                    "INSERT OR REPLACE INTO pricing_migration(version,status,started_at,completed_at,error) VALUES(?,?,?,?,?)",
                    (version, 'failed', started, self._timestamp(), str(exc)[:500]),
                )
            raise
        return {'version': version, 'migrated': True, 'conflicts': conflicts}

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
        canonical_models = set(self.canonical_models())
        rows = self.conn.execute("SELECT fingerprint,provider_type,source_json FROM source_provider").fetchall()
        created = []
        with self.conn:
            for row in rows:
                for model in self._broker_models_for_row(row):
                    if model not in canonical_models:
                        continue
                    inserted = self.conn.execute(
                        "INSERT OR IGNORE INTO provider_health(fingerprint,model,next_probe_at,updated_at) VALUES(?,?,?,?)",
                        (row["fingerprint"], model, stamp, stamp),
                    ).rowcount
                    if inserted:
                        created.append((row["fingerprint"], model))
        return created

    def health(self, fingerprint: str, model: str) -> dict:
        fingerprint = self._resolve_single_key_identifier(fingerprint)
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
        fingerprint = self._resolve_single_key_identifier(fingerprint)
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
        fingerprint = self._resolve_single_key_identifier(fingerprint)
        with self.conn:
            self.conn.execute("""INSERT INTO probe_event(fingerprint,model,tier,mode,reachable,responded,first_token,model_matched,ttfb_ms,ttft_ms,duration_ms,error_type,error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (fingerprint, model, tier, mode, int(reachable), int(responded), int(first_token), int(model_matched), ttfb_ms, ttft_ms, duration_ms, error_type, error, self._timestamp(now)))

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
        pricing_settings = ("pricing:", "pricing-provider:", "model:", "relay-binding:", "key-mapping:")
        if (setting not in safe_settings and not setting.startswith(pricing_settings)) or before == after:
            return
        transaction = nullcontext() if self.conn.in_transaction else self.conn
        with transaction:
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
                         role: str | None = None, stage: str | None = None,
                         currency: str | None = None, multiplier: float | None = None,
                         price: float | None = None, price_source: str | None = None,
                         price_comparable: bool | None = None) -> None:
        reasons = {"policy_disabled", "inventory_mismatch", "capability_unsupported", "health_open",
                   "route_blocked", "site_disabled", "key_capacity", "site_capacity", "global_capacity",
                   "deadline_budget"}
        if exclusion_reason not in reasons:
            exclusion_reason = None if eligible else "inventory_mismatch"
        ordinal = self.conn.execute("SELECT coalesce(max(ordinal), -1)+1 FROM route_candidate WHERE route_id=?", (route_id,)).fetchone()[0]
        with self.conn:
            self.conn.execute("""INSERT INTO route_candidate(route_id,ordinal,fingerprint,model,site_id,eligible,
                exclusion_reason,initial_rank,launched,role,stage,currency,multiplier,price,price_source,price_comparable)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                route_id, ordinal, fingerprint, model, site_id, int(eligible), exclusion_reason,
                initial_rank, int(launched), role, stage, currency, multiplier, price, price_source,
                None if price_comparable is None else int(price_comparable),
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

    def pricing_health(self) -> dict:
        migration = self.conn.execute(
            "SELECT version,status,error,completed_at FROM pricing_migration ORDER BY version DESC LIMIT 1"
        ).fetchone()
        duplicate_active = self.conn.execute(
            """SELECT count(*) FROM (
                 SELECT provider_id,model_id FROM provider_model_price p
                 JOIN pricing_provider pp ON pp.id=p.provider_id
                 WHERE p.active=1 AND p.source_kind <> 'legacy-migration' AND p.legacy=0
                   AND pp.provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra')
                 GROUP BY provider_id,model_id HAVING count(*) > 1
               )"""
        ).fetchone()[0]
        unpriced_active = self.conn.execute(
            """SELECT count(*) FROM provider_model_price p
               JOIN pricing_provider pp ON pp.id=p.provider_id
               WHERE p.active=1 AND p.unpriced=1
                 AND p.source_kind <> 'legacy-migration' AND p.legacy=0
                 AND pp.provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra')"""
        ).fetchone()[0]
        mapping_count = self.conn.execute(
            "SELECT count(*) FROM key_model_mapping WHERE enabled=1"
        ).fetchone()[0]
        duplicate_mappings = self.conn.execute(
            "SELECT count(*) FROM (SELECT fingerprint,model_id FROM key_model_mapping WHERE enabled=1 GROUP BY fingerprint,model_id HAVING count(*)>1)"
        ).fetchone()[0]
        unpriced_mappings = self.conn.execute(
            """SELECT count(*) FROM key_model_mapping m
               JOIN provider_model_price p ON p.provider_id=m.target_provider_id
                AND p.model_id=m.target_model_id AND p.active=1
                AND p.source_kind <> 'legacy-migration' AND p.legacy=0
               JOIN pricing_provider pp ON pp.id=p.provider_id
                AND pp.provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra')
               WHERE m.enabled=1 AND p.unpriced=1"""
        ).fetchone()[0]
        dangling_mappings = self.conn.execute(
            """SELECT count(*) FROM key_model_mapping m
            LEFT JOIN pricing_provider p ON p.id=m.target_provider_id AND p.active=1
                AND p.provider_type IN ('openai','anthropic','deepseek','qwen','doubao','deepinfra')
               LEFT JOIN canonical_model cm ON cm.id=m.model_id
               LEFT JOIN canonical_model tm ON tm.id=m.target_model_id
            LEFT JOIN provider_model_price pp ON pp.provider_id=m.target_provider_id
                AND pp.model_id=m.target_model_id AND pp.active=1
                AND pp.source_kind <> 'legacy-migration' AND pp.legacy=0
               WHERE m.enabled=1 AND (p.id IS NULL OR cm.id IS NULL OR tm.id IS NULL OR pp.id IS NULL)"""
        ).fetchone()[0]
        # Source provenance and currency are no longer pricing-domain gates.
        missing_source_evidence = 0
        unsupported_currency_aggregation = self.conn.execute(
            "SELECT count(*) FROM observation WHERE cost IS NOT NULL AND NULLIF(trim(currency),'') IS NULL"
        ).fetchone()[0]
        missing_mappings = 0
        for source in self.conn.execute("SELECT fingerprint,models_json FROM source_provider"):
            try:
                source_models = json.loads(source["models_json"])
            except (TypeError, ValueError):
                source_models = []
            for raw_model in source_models:
                model_id = canonicalize(str(raw_model))
                if model_id == "unavailable" or not self.conn.execute(
                    "SELECT 1 FROM canonical_model WHERE id=? AND active=1", (model_id,)
                ).fetchone():
                    continue
                if not self.conn.execute(
                    "SELECT 1 FROM key_model_mapping WHERE fingerprint=? AND model_id=? AND enabled=1",
                    (source["fingerprint"], model_id),
                ).fetchone():
                    missing_mappings += 1
        fresh_seed = self.conn.execute(
            "SELECT 1 FROM broker_setting WHERE name='pricing_domain_seed_version'"
        ).fetchone()
        migration_payload = {
            "version": int(migration["version"]) if migration else (PRICING_MIGRATION_VERSION if fresh_seed else 0),
            "status": migration["status"] if migration else ("completed" if fresh_seed else "missing"),
            "error": migration["error"] if migration else None,
            "completed_at": migration["completed_at"] if migration else None,
        }
        return {
            "migration": migration_payload,
            "duplicate_active": int(duplicate_active),
            "unpriced_active": int(unpriced_active),
            "active_mapping_count": int(mapping_count),
            "duplicate_active_mappings": int(duplicate_mappings),
            "unpriced_mappings": int(unpriced_mappings),
            "dangling_mappings": int(dangling_mappings),
            "missing_mappings": int(missing_mappings),
            "missing_source_evidence": int(missing_source_evidence),
            "unsupported_currency_aggregation": int(unsupported_currency_aggregation),
            "startup_ready": migration_payload["version"] >= PRICING_MIGRATION_VERSION
                and migration_payload["status"] == "completed"
                and not migration_payload["error"]
                and duplicate_active == 0
                and duplicate_mappings == 0 and dangling_mappings == 0
                and missing_mappings == 0 and missing_source_evidence == 0
                and unsupported_currency_aggregation == 0,
        }

    def data_health(self) -> dict:
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
        } | self.pricing_health()

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
            eligible,exclusion_reason,initial_rank,launched,role,stage,currency,multiplier,price,price_source,
            price_comparable FROM route_candidate WHERE route_id=? ORDER BY ordinal""", (route_id,))]
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
                "ordinal,fingerprint,model,site_id,eligible,exclusion_reason,initial_rank,launched,role,stage,"
                "currency,multiplier,price,price_source,price_comparable")
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

    @staticmethod
    def _amplification_from_rows(attempts: list[dict], usage: list[dict]) -> dict:
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

    def route_amplification(self, route_id: str, attempts: list[dict] | None = None) -> dict:
        attempts = attempts if attempts is not None else [dict(row) for row in self.conn.execute(
            "SELECT * FROM route_attempt WHERE route_id=? ORDER BY attempt_number", (route_id,))]
        usage = [dict(row) for row in self.conn.execute("""SELECT fingerprint,status,input_tokens,output_tokens,cost FROM observation
            WHERE route_id=? AND attempt_number IS NOT NULL AND attempt_number>0""", (route_id,)).fetchall()]
        return self._amplification_from_rows(attempts, usage)

    def record_capability(self, fingerprint: str, model: str, contract: str, state: str,
                          failure_class: str | None = None) -> None:
        with self.conn:
            self.conn.execute("""INSERT INTO provider_capability(fingerprint,model,contract,state,last_failure_class,updated_at)
                VALUES(?,?,?,?,?,?) ON CONFLICT(fingerprint,model,contract) DO UPDATE SET
                state=excluded.state,last_failure_class=excluded.last_failure_class,updated_at=excluded.updated_at""",
                (fingerprint, model, contract, state, failure_class, self._timestamp()))
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
        parts = urlsplit(str(base_url).strip())
        host = (parts.hostname or "").rstrip(".").lower()
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            pass
        try:
            port = parts.port
        except ValueError:
            port = None
        default_port = (parts.scheme.lower() == "https" and port == 443) or (parts.scheme.lower() == "http" and port == 80)
        authority = host if not port or default_port else f"{host}:{port}"
        normalized = f"{parts.scheme.lower()}://{authority}{parts.path.rstrip('/')}"
        return hmac.new(b"provider-broker-source-v1", f"{normalized}\0{api_key}".encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def api_key_mask(api_key: str) -> str:
        if not isinstance(api_key, str) or len(api_key) < 7:
            return "***"
        return api_key[:3] + "***" + api_key[-3:]

    def replace_source_snapshot(self, entries: list[dict], synced_at: str):
        rows = []
        site_notes = []
        existing_rows = self.conn.execute("SELECT fingerprint,base_url,api_key,models_json FROM source_provider").fetchall()
        existing = {row["fingerprint"]: row for row in existing_rows}
        existing_policies = {row[0] for row in self.conn.execute("SELECT fingerprint FROM policy")}
        for entry in entries:
            base_url, api_key = entry["base_url"].rstrip("/"), entry["api_key"]
            transport_provider_type = entry.get("provider_type")
            explicit_provider_type = entry.get("canonical_provider_type") or entry.get("pricing_provider_type")
            inferred_provider_type = explicit_provider_type or (
                {"openai_chat": "openai", "anthropic_messages": "anthropic"}.get(
                    str(transport_provider_type).strip().lower()
                    if isinstance(transport_provider_type, str) else ""
                )
            ) or transport_provider_type or "openai"
            pricing_type = canonical_provider_type(
                inferred_provider_type,
                base_url=base_url, models=entry.get("models") or (),
            )
            if pricing_type is None:
                # CPA synchronization filters these entries before storage;
                # direct Store callers receive the same six-provider boundary
                # by ignoring unsupported inventory instead of aborting the
                # whole snapshot.
                continue
            from .catalog import canonicalize
            source_models = list(dict.fromkeys(canonicalize(model) for model in (entry.get("models") or [entry.get("model", "unavailable")])))
            # Inventory keeps the complete CPA model evidence. Routing still
            # applies its capability/stage gate later; pricing must not.
            models = [model for model in source_models if model != "unavailable"]
            # Stable fingerprints avoid decrypting credentials during ordinary
            # refreshes.  The fallback only supports one-time adoption of old
            # databases whose fingerprint included the previous model list.
            prior = existing.get(self.fingerprint(base_url, api_key))
            if prior is None:
                for candidate in existing_rows:
                    try:
                        if self.fingerprint(candidate["base_url"], self._decrypt(candidate["api_key"])) == self.fingerprint(base_url, api_key) and self._decrypt(candidate["api_key"]) == api_key:
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
                    models = [model for model in source_models if model != "unavailable"]
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
            if isinstance(transport_provider_type, str) and transport_provider_type.strip():
                source["transport_provider_type"] = transport_provider_type.strip()[:160]
            source["broker_provider_type"] = pricing_type
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
            # Keep the inventory row keyed by the canonical pricing provider.
            # The transport adapter remains available in source_json, while
            # Provider + Model pricing must always use the six-provider
            # identity that CPA supplied as canonical_provider_type.
            rows.append((fp, name, base_url, self._encrypt(api_key), pricing_type, pricing_type, self._encrypt(request_headers), self.api_key_mask(api_key), json.dumps(models), json.dumps(source), synced_at, site_id))
        canonical_model_metadata = self.canonical_models()
        current_models = {row[0]: set(json.loads(row[8])) for row in rows}
        preserved_mappings = [
            dict(item) for item in self.key_model_mappings()
            if item["fingerprint"] in current_models
            and item["model_id"] in current_models[item["fingerprint"]]
        ]
        with self.conn:
            self.conn.execute("CREATE TEMP TABLE incoming AS SELECT * FROM source_provider WHERE 0")
            self.conn.executemany("INSERT INTO incoming(fingerprint,name,base_url,api_key,provider_type,pricing_provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            # source_provider is replaced atomically.  Mapping rows are
            # relationship data and are rebuilt from the new snapshot below;
            # remove them first so the foreign key cannot block the replace.
            self.conn.execute("DELETE FROM key_model_mapping")
            self.conn.execute("DELETE FROM source_provider")
            self.conn.execute("INSERT INTO source_provider(fingerprint,name,base_url,api_key,provider_type,pricing_provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id) SELECT fingerprint,name,base_url,api_key,provider_type,pricing_provider_type,request_headers,api_key_mask,models_json,source_json,synced_at,site_id FROM incoming")
            self.conn.execute("DELETE FROM route_block WHERE fingerprint NOT IN (SELECT fingerprint FROM incoming)")
            self.conn.execute("DROP TABLE incoming")
            self.conn.executemany("INSERT OR IGNORE INTO policy(fingerprint) VALUES(?)", [(r[0],) for r in rows])
            self.conn.executemany("INSERT OR IGNORE INTO site_policy(site_id) VALUES(?)", [(r[11],) for r in rows])
            active_pairs = {
                (row[5], model)
                for row in rows
                for model in json.loads(row[8])
                if model != 'unavailable'
            }
            pricing_ids = [
                row['id'] for row in self.conn.execute(
                    "SELECT id FROM pricing_provider WHERE provider_key IN (?,?,?,?,?,?)",
                    tuple(sorted(PROVIDER_TYPES)),
                )
            ]
            if pricing_ids:
                placeholders = ','.join('?' for _ in pricing_ids)
                if active_pairs:
                    self.conn.execute(
                        f"UPDATE provider_model_price SET active=0 WHERE provider_id IN ({placeholders}) AND active=1 AND (provider_id,model_id) NOT IN ({','.join('(?,?)' for _ in active_pairs)})",
                        (*pricing_ids, *[part for pair in sorted(active_pairs) for part in pair]),
                    )
                else:
                    self.conn.execute(
                        f"UPDATE provider_model_price SET active=0 WHERE provider_id IN ({placeholders}) AND active=1",
                        tuple(pricing_ids),
                    )
            self.conn.executemany(
                "UPDATE policy SET calibrated=? WHERE fingerprint=?",
                [(int(any(model in canonical_model_metadata for model in json.loads(r[8]))), r[0]) for r in rows if r[0] not in existing_policies],
            )
            # A site name is a useful first-run label, but never overwrite an
            # operator's policy note during a later CPA refresh.
            self.conn.executemany("UPDATE policy SET note=? WHERE fingerprint=? AND note=''", site_notes)
            for row in self.conn.execute("""SELECT s.fingerprint,s.base_url,s.provider_type,s.pricing_provider_type,s.name,s.models_json
                                               FROM source_provider s JOIN policy p USING(fingerprint)"""):
                provider_type = canonical_provider_type(
                    row['pricing_provider_type'] or row['provider_type'],
                    base_url=row['base_url'], models=json.loads(row['models_json']),
                )
                if provider_type is None:
                    raise ValueError("unsupported provider type")
                key = provider_type
                provider_row = self.conn.execute(
                    "SELECT id,provider_key FROM pricing_provider WHERE provider_key=? AND provider_type=?",
                    (key, provider_type),
                ).fetchone()
                if provider_row is None:
                    provider_row = self.conn.execute(
                        "SELECT id,provider_key FROM pricing_provider WHERE provider_type=? AND active=1 ORDER BY id LIMIT 1",
                        (provider_type,),
                    ).fetchone()
                if provider_row is None:
                    self.conn.execute(
                        "INSERT INTO pricing_provider(provider_key,name,provider_type,multiplier,active) VALUES(?,?,?,?,1)",
                        (key, row['name'], provider_type, 1.0),
                    )
                    provider_row = self.conn.execute("SELECT id,provider_key FROM pricing_provider WHERE provider_key=?", (key,)).fetchone()
                provider_id = provider_row['id']
                self.conn.execute("UPDATE pricing_provider SET active=1 WHERE id=?", (provider_id,))
                self.conn.execute("UPDATE source_provider SET pricing_provider_id=?,pricing_provider_type=? WHERE fingerprint=?", (provider_id, provider_type, row['fingerprint']))
                for model_id in json.loads(row['models_json']):
                    model_id = canonicalize(model_id)
                    if model_id == 'unavailable':
                        continue
                    target_id, target_model = provider_id, model_id
                    prior_mapping = next(
                        (item for item in preserved_mappings
                         if item['fingerprint'] == row['fingerprint'] and item['model_id'] == model_id),
                        None,
                    )
                    mapping_multiplier = float(prior_mapping['multiplier']) if prior_mapping else 1.0
                    existing_price = self.conn.execute(
                        "SELECT 1 FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=1",
                        (provider_id, model_id),
                    ).fetchone()
                    if not existing_price:
                        inactive_price = self.conn.execute(
                            "SELECT id FROM provider_model_price WHERE provider_id=? AND model_id=? AND active=0 ORDER BY id DESC LIMIT 1",
                            (provider_id, model_id),
                        ).fetchone()
                        if inactive_price:
                            self.conn.execute("UPDATE provider_model_price SET active=1 WHERE id=?", (inactive_price['id'],))
                        else:
                            legacy_price = self.conn.execute(
                                """SELECT p.output_price_cny,p.output_price,p.unpriced
                                   FROM provider_model_price p JOIN pricing_provider old ON old.id=p.provider_id
                                   WHERE old.provider_key LIKE 'key-source:%' AND p.model_id=? AND p.active=1
                                   ORDER BY p.id DESC LIMIT 1""",
                                (model_id,),
                            ).fetchone()
                            preserved_output = legacy_price['output_price_cny'] if legacy_price and legacy_price['output_price_cny'] is not None else legacy_price['output_price'] if legacy_price else DEFAULT_OUTPUT_PRICE_CNY
                            self.insert_provider_model_price(
                                provider_id=provider_id, model_id=model_id,
                                output_price_cny=preserved_output,
                                source_name='pricing-domain-migration' if legacy_price else 'cpa-sync-default',
                                source_evidence='Preserved existing Provider+Model price during CPA identity migration' if legacy_price else 'Default CNY output price for a newly discovered CPA combination',
                                unpriced=bool(legacy_price['unpriced']) if legacy_price else False,
                            )
                    self.upsert_key_model_mapping(
                        row['fingerprint'], model_id, target_provider_id=target_id,
                        target_model_id=target_model, multiplier=mapping_multiplier, enabled=True,
                    )
            # A source refresh must not overwrite an administrator's editable
            # target or multiplier. Reapply mappings whose referenced target is
            # still valid; newly discovered key/model pairs keep the defaults
            # materialized above.
            for mapping in preserved_mappings:
                try:
                    self.upsert_key_model_mapping(
                        mapping['fingerprint'], mapping['model_id'],
                        target_provider_id=mapping['target_provider_id'],
                        target_model_id=mapping['target_model_id'],
                        multiplier=float(mapping['multiplier']), enabled=bool(mapping['enabled']),
                    )
                except (ValueError, sqlite3.IntegrityError):
                    # If an old target identity was retired, retain the
                    # operator's multiplier on the newly derived mapping.
                    fallback = self.conn.execute(
                        "SELECT id FROM key_model_mapping WHERE fingerprint=? AND model_id=? AND enabled=1",
                        (mapping['fingerprint'], mapping['model_id']),
                    ).fetchone()
                    if fallback:
                        self.update_key_model_mapping(
                            fallback['id'], multiplier=float(mapping['multiplier']),
                            enabled=bool(mapping['enabled']),
                        )

    @staticmethod
    def site_id(site_name: object, base_url: str) -> str:
        """Stable grouping key from URL.hostname, capped at three labels."""
        return normalize_hostname(base_url) or "default"

    def capability_allows_route(self, fingerprint: str, model: str, contract: str) -> bool:
        """Gate only the contract known to be unsupported for this key/model."""
        row = self.conn.execute(
            "SELECT state FROM provider_capability WHERE fingerprint=? AND model=? AND contract=?",
            (fingerprint, model, contract),
        ).fetchone()
        return row is None or row["state"] != "unsupported"

    def _provider_from_row(self, row, model: str, canonical_model_metadata: dict) -> Provider:
        headers = json.loads(self._decrypt(row['request_headers'])) if row['request_headers'] else {}
        source = json.loads(row['source_json']) if row['source_json'] else {}
        broker_provider_type = self._broker_provider_type_for_row(row)
        # Broker's fixed mapping is authoritative. CPA /models aliases are
        # discovery evidence only and may be absent when inventory failed.
        wire_model = BROKER_PROVIDER_MODELS.get(broker_provider_type, {}).get(model)
        model_aliases = source.get('model_aliases') or {}
        if wire_model is None:
            wire_model = model_aliases.get(model)
        reverse_aliases = {str(wire).casefold(): str(alias) for alias, wire in model_aliases.items()}
        source_models = self._broker_models_for_row(row)
        pricing_by_model = {
            candidate: self.effective_key_pricing(row['fingerprint'], candidate)
            for candidate in source_models
            if candidate in canonical_model_metadata
        }
        pricing = pricing_by_model.get(model) or self.effective_key_pricing(row['fingerprint'], model)
        pricing_by_model = pricing_by_model | {model: pricing}
        price_group = int(pricing['blended_price'] * 100000) if pricing['priced'] else None
        transport_provider_type = source.get("transport_provider_type") or row['provider_type']
        return Provider(
            row['id'], row['fingerprint'], row['name'], row['base_url'], self._decrypt(row['api_key']),
            transport_provider_type, headers, [model], pricing, price_group, int(row['max_parallel']),
            bool(row['enabled']), row['site_id'], wire_model, reverse_aliases,
            pricing_by_model=pricing_by_model, price_currency=pricing['currency'],
            price_comparable=bool(pricing['priced']), price_source=pricing['source'],
            price_reason=pricing['reason'],
        )

    @staticmethod
    def _broker_provider_type_for_row(row) -> str:
        try:
            source = json.loads(row['source_json'] or '{}')
        except (TypeError, ValueError):
            source = {}
        provider_type = str(
            source.get('broker_provider_type') or row['provider_type'] or ''
        ).strip().lower()
        return {
            'openai_chat': 'openai',
            'anthropic_messages': 'anthropic',
        }.get(provider_type, provider_type)

    @classmethod
    def _broker_models_for_row(cls, row) -> list[str]:
        """Return Broker-owned models for one key, independent of CPA inventory."""
        provider_type = cls._broker_provider_type_for_row(row)
        return list(BROKER_PROVIDER_MODELS.get(provider_type, {}))

    def providers(self, tier: str, *, contract: str | None = None) -> list[Provider]:
        rows = self.conn.execute("""SELECT s.*,p.enabled,p.calibrated,p.tiers_json,p.max_parallel FROM source_provider s JOIN policy p USING(fingerprint)
        WHERE p.enabled=1 AND p.calibrated=1 ORDER BY s.id""").fetchall()
        canonical_model_metadata = self.canonical_models()
        result=[]
        for r in rows:
            blocked={row[0] for row in self.conn.execute('SELECT model FROM route_block WHERE fingerprint=?',(r['fingerprint'],))}
            models=[m for m in self._broker_models_for_row(r) if m in canonical_model_metadata and canonical_model_metadata[m]['stage'] == tier and m not in blocked]
            if models and tier in json.loads(r['tiers_json']):
                # A key can expose several catalog models in the same stage.  Health is
                # per model, so make each routing candidate explicit rather than letting
                # an open model hide behind the first item in a shared list.
                for model in models:
                    if contract and not self.capability_allows_route(r['fingerprint'], model, contract):
                        continue
                    if not self.health_allows_route(r['fingerprint'], model):
                        continue
                    result.append(self._provider_from_row(r, model, canonical_model_metadata))
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
            JOIN canonical_model c ON c.id=h.model AND c.active=1
            WHERE h.state='open' AND p.enabled=1 AND p.calibrated=1 AND c.stage=?
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
        row = self.conn.execute("""SELECT s.*,p.enabled,p.calibrated,p.tiers_json,p.max_parallel
            FROM source_provider s JOIN policy p USING(fingerprint) WHERE s.fingerprint=?""", (fingerprint,)).fetchone()
        catalog = self.canonical_models()
        if row is None or not row['enabled'] or not row['calibrated'] or model not in self._broker_models_for_row(row) or model not in catalog:
            return None
        tier = catalog[model]['stage']
        if tier not in json.loads(row['tiers_json']):
            return None
        return self._provider_from_row(row, model, catalog)

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
        rows = self.conn.execute("SELECT s.*,p.enabled,p.calibrated,p.note,p.max_parallel,p.tiers_json FROM source_provider s JOIN policy p USING(fingerprint) ORDER BY s.id").fetchall()
        inventory = []
        for row in rows:
            models = json.loads(row['models_json'])
            model_pricing = {
                model: self.effective_key_pricing(row['fingerprint'], model)
                for model in models
            }
            stats = self.conn.execute(
                "SELECT avg(success) rate, avg(latency_ms) ttft, sum(cost) cost, sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL THEN input_tokens + output_tokens END) total_tokens FROM observation WHERE fingerprint=? AND created_at>=datetime('now',?)",
                (row['fingerprint'], modifier),
            ).fetchone()
            accounting = self.accounting(window=window, fingerprint=row['fingerprint'])
            fees = accounting["fee_buckets"]
            single_fee = next(iter(fees.values()), {}).get("total_fee") if len(fees) == 1 else None
            latest = self.conn.execute("""SELECT evidence_at,ttft_ms,status FROM (
                    SELECT created_at evidence_at,latency_ms ttft_ms,status,1 source_priority
                    FROM observation WHERE fingerprint=? AND latency_ms IS NOT NULL
                    UNION ALL
                    SELECT created_at evidence_at,ttft_ms,COALESCE(error_type,'completed') status,0 source_priority
                    FROM probe_event WHERE fingerprint=?
                ) ORDER BY julianday(evidence_at) DESC,source_priority DESC LIMIT 1""",
                (row['fingerprint'], row['fingerprint']),
            ).fetchone()
            try:
                source_metadata = json.loads(row['source_json'] or "{}")
            except (TypeError, ValueError):
                source_metadata = {}
            inventory.append({
                'fingerprint': row['fingerprint'], 'name': row['name'], 'base_url': row['base_url'],
                'site_id': row['site_id'], 'normalized_hostname': row['site_id'],
                'family': source_metadata.get('transport_provider_type') or row['provider_type'],
                'api_key_mask': row['api_key_mask'] or '***', 'models': models,
                'model_pricing': model_pricing,
                'inventory_status': source_metadata.get('inventory_status'), 'enabled': bool(row['enabled']),
                'calibrated': bool(row['calibrated']), 'note': row['note'], 'max_parallel': row['max_parallel'],
                'technical_success_rate': stats['rate'], 'avg_ttft_ms': stats['ttft'],
                'cost_24h': single_fee, 'total_tokens': accounting['total_tokens'], 'fee_buckets': fees,
                'accounting': accounting, 'tiers': json.loads(row['tiers_json']), 'synced_at': row['synced_at'],
                'last_test_at': latest['evidence_at'] if latest else None,
                'last_test_ttft_ms': latest['ttft_ms'] if latest else None,
                'last_test_status': latest['status'] if latest else None,
            })
        return inventory

    def api_key_resources(self, window: str = "24h") -> list[dict]:
        """Return the credential-facing admin contract.

        The routing inventory above intentionally remains rich because it is
        consumed by internal routing and migration code.  This projection is
        the only shape used by the Key console: it never includes endpoint
        paths, provider/model inventory, pricing, or health statistics.  Each
        credential is one row; the console merges only the repeated hostname
        cell visually.
        """
        if window not in ACCOUNTING_WINDOWS:
            raise ValueError("invalid window")
        rows = self.conn.execute(
            """SELECT s.id,s.fingerprint,s.base_url,s.api_key_mask,s.source_json,
                      p.enabled,p.note,p.max_parallel
                 FROM source_provider s JOIN policy p USING(fingerprint)
                 ORDER BY s.id"""
        ).fetchall()
        model_resources = {}
        for item in self.stage_resources(window):
            model_resources.setdefault(item["fingerprint"], []).append({
                "model": item["model"], "stage": item["stage"], "family": item["family"],
                "callable": item["callable"], "latest_test": item["latest_test"],
                "technical_success_rate": item["technical_success_rate"],
                "avg_first_token_latency_ms": item["avg_first_token_latency_ms"],
                "total_tokens": item["total_tokens"], "fee_buckets": item["fee_buckets"],
            })
        resources = []
        for row in rows:
            hostname = normalize_hostname(row["base_url"]) or "UNKNOWN"
            try:
                source = json.loads(row["source_json"] or "{}")
            except (TypeError, ValueError):
                source = {}
            inventory_status = source.get("inventory_status")
            status = "disabled" if not row["enabled"] else (
                "unavailable" if inventory_status not in (None, "available", "stale") else "enabled"
            )
            accounting = self.accounting(window=window, fingerprint=row["fingerprint"])
            resources.append({
                "fingerprint": row["fingerprint"],
                "normalized_hostname": hostname,
                "status": status,
                "note": row["note"],
                "api_key_mask": row["api_key_mask"] or "***",
                "max_parallel": row["max_parallel"],
                "window": window,
                "total_tokens": accounting["total_tokens"],
                "fee_buckets": accounting["fee_buckets"],
                "models": model_resources.get(row["fingerprint"], []),
                "edit": {
                    "fingerprint": row["fingerprint"],
                    "href": f"/admin/v1/keys/{row['fingerprint']}",
                },
            })
        return resources

    def key_test_provider(self, fingerprint: str) -> tuple[Provider | None, str | None]:
        """Choose the cheapest callable active model for one API Key."""
        catalog = self.canonical_models()
        candidates = []
        for stage in ("standard", "smart", "expert"):
            for model in self.stage_models(stage):
                rows = [row for row in self._stage_key_rows(stage, model, callable_only=True)
                        if row["fingerprint"] == fingerprint]
                if not rows or model not in catalog:
                    continue
                candidates.append((stage, model, rows[0]))
        if not candidates:
            return None, "API Key is disabled, unavailable, or has no callable model"
        priced = []
        for stage, model, row in candidates:
            pricing = self.effective_key_pricing(fingerprint, model)
            if pricing.get("priced") and pricing.get("currency") == "CNY":
                priced.append((float(pricing["output_price_cny"]), stage, model, row))
        if priced:
            _, stage, model, row = min(
                priced,
                key=lambda item: (item[0], ("standard", "smart", "expert").index(item[1]), item[2]),
            )
        else:
            stage, model, row = candidates[0]
        return self._provider_from_row(row, model, catalog), stage

    @staticmethod
    def _merge_accounting(reports: list[dict], *, window: str) -> dict:
        """Merge already-windowed accounting reports without rewriting history."""
        fee_buckets: dict[str, dict] = {}
        observed_calls = 0
        for report in reports:
            for currency, item in (report.get("fee_buckets") or {}).items():
                target = fee_buckets.setdefault(currency, {
                    "currency": currency, "tokens": 0, "total_tokens": 0,
                    "fee": None, "total_fee": None, "calls": 0,
                })
                target["tokens"] += int(item.get("tokens", item.get("total_tokens", 0)) or 0)
                target["total_tokens"] += int(item.get("total_tokens", item.get("tokens", 0)) or 0)
                target["calls"] += int(item.get("calls", 0) or 0)
                observed_calls += int(item.get("calls", 0) or 0)
                fee = item.get("total_fee", item.get("fee"))
                if isinstance(fee, (int, float)):
                    target["fee"] = (target["fee"] or 0) + fee
                    target["total_fee"] = target["fee"]
        total_tokens = sum(item["total_tokens"] for item in fee_buckets.values())
        return {
            "window": window,
            "total_tokens": total_tokens if observed_calls else None,
            "token_total": total_tokens if observed_calls else None,
            "fee_buckets": fee_buckets,
            "fees_by_currency": {key: item["total_fee"] for key, item in fee_buckets.items()},
            "tokens_by_currency": {key: item["total_tokens"] for key, item in fee_buckets.items()},
        }

    def _api_key_group_members(self, identifier: str) -> list[sqlite3.Row]:
        """Resolve a normalized group id or an individual pre-cutover fingerprint."""
        exact = self.conn.execute(
            """SELECT s.id,s.fingerprint,s.base_url,s.api_key_mask,s.source_json,
                      p.enabled,p.note,p.max_parallel
                 FROM source_provider s JOIN policy p USING(fingerprint)
                WHERE s.fingerprint=? ORDER BY s.id""", (identifier,)
        ).fetchall()
        if exact:
            return exact
        hostname = normalize_hostname(identifier) or "UNKNOWN"
        rows = self.conn.execute(
            """SELECT s.id,s.fingerprint,s.base_url,s.api_key_mask,s.source_json,
                      p.enabled,p.note,p.max_parallel
                 FROM source_provider s JOIN policy p USING(fingerprint)
                ORDER BY s.id"""
        ).fetchall()
        return [
            row for row in rows
            if (normalize_hostname(row["base_url"]) or "UNKNOWN") == hostname
        ]

    def api_key_group_fingerprints(self, identifier: str) -> list[str]:
        return [row["fingerprint"] for row in self._api_key_group_members(identifier)]

    def _resolve_single_key_identifier(self, identifier: str) -> str:
        """Accept the grouped API identifier for single-key operational calls."""
        if self.conn.execute(
            "SELECT 1 FROM source_provider WHERE fingerprint=?", (identifier,)
        ).fetchone():
            return identifier
        members = self._api_key_group_members(identifier)
        return members[0]["fingerprint"] if len(members) == 1 else identifier

    def api_key_resource(self, fingerprint: str, window: str = "24h", *, include_mappings: bool = False) -> dict | None:
        """Return one safe Key resource, optionally with mapping edit data."""
        members = self._api_key_group_members(fingerprint)
        if not members:
            return None
        resource = next((item for item in self.api_key_resources(window)
                         if item["fingerprint"] == members[0]["fingerprint"]), None)
        if resource is None:
            return None
        if include_mappings:
            mappings = []
            for member in members:
                mappings.extend(self.key_model_mappings(fingerprint=member["fingerprint"], enabled=None))
            resource["edit"] = resource["edit"] | {
                "mappings": [
                    {key: mapping[key] for key in (
                        "id", "model_id", "target_provider_id", "target_provider_key",
                        "target_model_id", "multiplier", "enabled",
                    )}
                    for mapping in mappings
                ]
            }
        return resource

    def _stage_key_rows(self, stage: str, model: str, *, callable_only: bool = False) -> list[sqlite3.Row]:
        """Find configured keys declaring a canonical Stage/Model pair.

        Aggregates use every declared key so historical evidence does not
        disappear when a key is disabled or its inventory refresh goes stale;
        callable counts and manual tests opt into the stricter live filter.
        """
        model = canonicalize(model)
        rows = self.conn.execute(
            """SELECT s.*,p.enabled,p.calibrated,p.tiers_json,p.max_parallel
                 FROM source_provider s JOIN policy p USING(fingerprint)"""
        ).fetchall()
        result = []
        for row in rows:
            try:
                models = set(self._broker_models_for_row(row))
                tiers = set(json.loads(row["tiers_json"] or "[]"))
                source = json.loads(row["source_json"] or "{}")
            except (TypeError, ValueError):
                models, tiers, source = set(), set(), {}
            if model not in models or stage not in tiers:
                continue
            if not callable_only:
                result.append(row)
                continue
            # CPA inventory is advisory. An unavailable /models response must
            # not remove a Broker-owned model from the callable key set.
            if not row["enabled"] or not row["calibrated"]:
                continue
            site = self.conn.execute(
                "SELECT enabled FROM site_policy WHERE site_id=?", (row["site_id"],)
            ).fetchone()
            if site is not None and not site["enabled"]:
                continue
            blocked = self.conn.execute(
                "SELECT 1 FROM route_block WHERE fingerprint=? AND model=?", (row["fingerprint"], model)
            ).fetchone()
            if blocked:
                continue
            if self.health(row["fingerprint"], model)["state"] == "open":
                continue
            result.append(row)
        return result

    def stage_test_providers(self, stage: str) -> list[Provider]:
        """Return enabled Key/Model capability pairs for a Stage test.

        The console operates on a Stage and its fixed primary model.  Runtime
        routing may still enumerate the wider canonical catalog separately.
        """
        catalog = self.canonical_models()
        candidates = self.stage_models(stage)
        if not candidates:
            return []
        pairs = []
        for candidate in candidates:
            if candidate not in catalog:
                continue
            pairs.extend((row, candidate) for row in self._stage_key_rows(stage, candidate, callable_only=True))
        result = []
        seen = set()
        for row, candidate in pairs:
            identity = (row["fingerprint"], candidate)
            if identity in seen:
                continue
            seen.add(identity)
            provider = self._provider_from_row(row, candidate, catalog)
            if provider is not None:
                result.append(provider)
        return result

    def _stage_accounting(self, stage: str, model: str, window: str, fingerprints: list[str]) -> dict:
        if not fingerprints:
            return {"window": window, "boundary": ACCOUNTING_WINDOWS[window], "stage": stage,
                    "model": canonicalize(model), "total_tokens": 0, "token_total": 0,
                    "fee_buckets": {}, "fees_by_currency": {}, "tokens_by_currency": {}}
        placeholders = ",".join("?" for _ in fingerprints)
        rows = self.conn.execute(
            f"""SELECT COALESCE(NULLIF(upper(currency),''),'UNKNOWN') currency,
                         COALESCE(sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                                         THEN input_tokens + output_tokens ELSE 0 END),0) total_tokens,
                         sum(cost) total_fee, count(*) calls
                    FROM observation
                   WHERE created_at >= datetime('now',?) AND tier=?
                     AND COALESCE(actual_model,requested_model)=?
                     AND fingerprint IN ({placeholders})
                   GROUP BY 1 ORDER BY 1""",
            [ACCOUNTING_WINDOWS[window], stage, canonicalize(model), *fingerprints],
        ).fetchall()
        buckets = {
            row["currency"]: {
                "currency": row["currency"], "tokens": int(row["total_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0), "fee": row["total_fee"],
                "total_fee": row["total_fee"], "calls": int(row["calls"] or 0),
            }
            for row in rows
        }
        total_tokens = sum(item["total_tokens"] for item in buckets.values())
        return {"window": window, "boundary": ACCOUNTING_WINDOWS[window], "stage": stage,
                "model": canonicalize(model), "total_tokens": total_tokens, "token_total": total_tokens,
                "fee_buckets": buckets,
                "fees_by_currency": {key: item["total_fee"] for key, item in buckets.items()},
                "tokens_by_currency": {key: item["total_tokens"] for key, item in buckets.items()}}

    def stage_resources(self, window: str = "24h") -> list[dict]:
        """Return one management row per fixed Stage and configured API Key."""
        if window not in ACCOUNTING_WINDOWS:
            raise ValueError("invalid window")
        canonical_model_metadata = self.canonical_models()
        rows = self.conn.execute(
            """SELECT s.*,p.enabled,p.note,p.max_parallel,p.tiers_json
                 FROM source_provider s JOIN policy p USING(fingerprint)
                ORDER BY s.id"""
        ).fetchall()
        resources = []
        stage_order = ("standard", "smart", "expert")
        for stage in stage_order:
            for model in self.stage_models(stage):
                metadata = canonical_model_metadata.get(model)
                if metadata is None:
                    continue
                for row in rows:
                    try:
                        declared = set(self._broker_models_for_row(row))
                        tiers = set(json.loads(row["tiers_json"] or "[]"))
                        source = json.loads(row["source_json"] or "{}")
                    except (TypeError, ValueError):
                        declared, tiers, source = set(), set(), {}
                    if model not in declared or stage not in tiers:
                        continue
                    fingerprint = row["fingerprint"]
                    accounting = self.accounting(window=window, fingerprint=fingerprint, stage=stage, model=model)
                    stats, _ = self._stage_scope_stats(stage, [model], [fingerprint], window)
                    callable_now = any(
                        item["fingerprint"] == fingerprint
                        for item in self._stage_key_rows(stage, model, callable_only=True)
                    )
                    latest = self._latest_stage_test([model], [fingerprint])
                    inventory_status = source.get("inventory_status")
                    status = "disabled" if not row["enabled"] else (
                        "unavailable" if inventory_status not in (None, "available", "stale") else "enabled"
                    )
                    provider_type = canonical_provider_type(
                        row["pricing_provider_type"] or row["provider_type"],
                        base_url=row["base_url"],
                    ) or "UNKNOWN"
                    resources.append({
                        "stage": stage,
                        "fingerprint": fingerprint,
                        "note": row["note"],
                        "model": model,
                        "family": metadata["family"],
                        "provider_type": provider_type,
                        "normalized_hostname": normalize_hostname(row["base_url"]) or "UNKNOWN",
                        "api_key_mask": row["api_key_mask"] or "***",
                        "status": status,
                        "max_parallel": row["max_parallel"],
                        "callable": callable_now,
                        "latest_test": latest,
                        "technical_success_rate": stats["rate"],
                        "avg_first_token_latency_ms": stats["ttft"],
                        "window": window,
                        "total_tokens": accounting["total_tokens"],
                        "fee_buckets": accounting["fee_buckets"],
                        "edit": {"fingerprint": fingerprint, "href": f"/admin/v1/keys/{fingerprint}"},
                    })
        return resources

    def _stage_scope_stats(self, stage: str, models: list[str], fingerprints: list[str], window: str) -> tuple[dict, dict]:
        if not models or not fingerprints:
            return {"rate": None, "ttft": None}, {
                "window": window, "total_tokens": None, "token_total": None,
                "fee_buckets": {}, "fees_by_currency": {}, "tokens_by_currency": {},
            }
        placeholders = ",".join("?" for _ in fingerprints)
        rows = self.conn.execute(
            f"""SELECT success,latency_ms,input_tokens,output_tokens,cost,currency,actual_model,requested_model
                   FROM observation
                  WHERE created_at >= datetime('now',?) AND tier=?
                    AND fingerprint IN ({placeholders})""",
            [ACCOUNTING_WINDOWS[window], stage, *fingerprints],
        ).fetchall()
        model_set = {canonicalize(model) for model in models}
        rows = [row for row in rows if canonicalize(row["actual_model"] or row["requested_model"]) in model_set]
        if not rows:
            return {"rate": None, "ttft": None}, {
                "window": window, "total_tokens": None, "token_total": None,
                "fee_buckets": {}, "fees_by_currency": {}, "tokens_by_currency": {},
            }
        success_values = [float(row["success"]) for row in rows if row["success"] is not None]
        latency_values = [float(row["latency_ms"]) for row in rows if row["latency_ms"] is not None]
        buckets: dict[str, dict] = {}
        for row in rows:
            currency = str(row["currency"] or "UNKNOWN").strip().upper() or "UNKNOWN"
            bucket = buckets.setdefault(currency, {
                "currency": currency, "tokens": 0, "total_tokens": 0,
                "fee": None, "total_fee": None, "calls": 0,
            })
            bucket["calls"] += 1
            if row["input_tokens"] is not None and row["output_tokens"] is not None:
                bucket["tokens"] += int(row["input_tokens"]) + int(row["output_tokens"])
                bucket["total_tokens"] = bucket["tokens"]
            if isinstance(row["cost"], (int, float)):
                bucket["fee"] = (bucket["fee"] or 0) + float(row["cost"])
                bucket["total_fee"] = bucket["fee"]
        total_tokens = sum(item["total_tokens"] for item in buckets.values())
        accounting = {
            "window": window, "total_tokens": total_tokens, "token_total": total_tokens,
            "fee_buckets": buckets,
            "fees_by_currency": {key: item["total_fee"] for key, item in buckets.items()},
            "tokens_by_currency": {key: item["total_tokens"] for key, item in buckets.items()},
        }
        return {
            "rate": sum(success_values) / len(success_values) if success_values else None,
            "ttft": sum(latency_values) / len(latency_values) if latency_values else None,
        }, accounting

    def _latest_stage_test(self, models: list[str], fingerprints: list[str]) -> dict | None:
        if not models or not fingerprints:
            return None
        model_placeholders = ",".join("?" for _ in models)
        fingerprint_placeholders = ",".join("?" for _ in fingerprints)
        row = self.conn.execute(
            f"""SELECT created_at,ttft_ms,
                              CASE WHEN first_token=1 AND model_matched=1 THEN 'succeeded'
                                   ELSE COALESCE(error_type,'failed') END status
                           FROM probe_event
                          WHERE model IN ({model_placeholders})
                            AND fingerprint IN ({fingerprint_placeholders})
                          ORDER BY id DESC LIMIT 1""",
            [*models, *fingerprints],
        ).fetchone()
        return None if row is None else {
            "at": row["created_at"], "status": row["status"], "ttft_ms": row["ttft_ms"],
        }

    def update_policy(self, fingerprint: str, body: dict):
        if not isinstance(body, dict) or set(body) - {"enabled", "calibrated", "note", "max_parallel", "tiers"}:
            raise ValueError("legacy policy pricing fields are not supported")
        with self.conn:
            current=self.conn.execute('SELECT * FROM policy WHERE fingerprint=?',(fingerprint,)).fetchone()
            if current is None: return False
            self.conn.execute("UPDATE policy SET enabled=?,calibrated=?,note=?,max_parallel=?,tiers_json=? WHERE fingerprint=?", (int(body.get("enabled",current['enabled'])),int(body.get('calibrated',current['calibrated'])),str(body.get('note',current['note'])),int(body.get('max_parallel',current['max_parallel'])),json.dumps(body.get("tiers",json.loads(current['tiers_json']))),fingerprint))
        return True

    def observe(self, **data):
        if data.get("fingerprint"):
            data = data | {"fingerprint": self._resolve_single_key_identifier(data["fingerprint"])}
        payload = {
            'diagnostic_json': None, 'route_id': None, 'attempt_number': None,
            'started_ms': None, 'elapsed_ms': None, 'currency': None,
            'input_tokens': None, 'output_tokens': None, 'cost': None,
            'request_id': None, 'actual_model': None, 'effort': None,
            'latency_ms': None, 'error': None, 'status': 'completed',
        } | data
        diagnostic = payload.pop("diagnostic", None)
        if diagnostic:
            payload["diagnostic_json"] = json.dumps(diagnostic, sort_keys=True)
        with self.conn:
            self.conn.execute("""INSERT INTO observation(
                fingerprint,requested_model,actual_model,tier,effort,success,latency_ms,error,status,
                input_tokens,output_tokens,cost,currency,request_id,diagnostic_json,route_id,attempt_number,started_ms,elapsed_ms
            ) VALUES(
                :fingerprint,:requested_model,:actual_model,:tier,:effort,:success,:latency_ms,:error,:status,
                :input_tokens,:output_tokens,:cost,:currency,:request_id,:diagnostic_json,:route_id,:attempt_number,:started_ms,:elapsed_ms
            )""", payload)

    def accounting(self, *, window: str = "24h", fingerprint: str | None = None,
                   stage: str | None = None, model: str | None = None,
                   canonical_model: str | None = None) -> dict:
        """Return token and fee totals in independent currency buckets."""
        modifier = ACCOUNTING_WINDOWS[window]
        clauses = ["created_at >= datetime('now', ?)"]
        params: list[object] = [modifier]
        if fingerprint is not None:
            clauses.append("fingerprint=?")
            params.append(fingerprint)
        if stage is not None:
            clauses.append("tier=?")
            params.append(stage)
        model = canonical_model or model
        if model is not None:
            clauses.append("COALESCE(actual_model,requested_model)=?")
            params.append(canonicalize(model))
        rows = self.conn.execute(
            """SELECT COALESCE(NULLIF(upper(currency),''),'UNKNOWN') currency,
                      COALESCE(sum(CASE WHEN input_tokens IS NOT NULL AND output_tokens IS NOT NULL
                                      THEN input_tokens + output_tokens ELSE 0 END),0) total_tokens,
                      sum(cost) total_fee, count(*) calls
                 FROM observation WHERE """ + " AND ".join(clauses) + " GROUP BY 1 ORDER BY 1",
            params,
        ).fetchall()
        buckets = {
            row["currency"]: {
                "currency": row["currency"], "tokens": int(row["total_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0), "fee": row["total_fee"],
                "total_fee": row["total_fee"], "calls": int(row["calls"] or 0),
            }
            for row in rows
        }
        total_tokens = sum(item["total_tokens"] for item in buckets.values())
        return {
            "window": window, "boundary": modifier, "fingerprint": fingerprint,
            "stage": stage, "model": model, "total_tokens": total_tokens,
            "token_total": total_tokens, "fee_buckets": buckets,
            "fees_by_currency": {key: item["total_fee"] for key, item in buckets.items()},
            "tokens_by_currency": {key: item["total_tokens"] for key, item in buckets.items()},
        }

    def quality(self, window='24h'):
        modifier={'1h':'-1 hour','24h':'-24 hours','7d':'-7 days','30d':'-30 days'}[window]
        where="created_at >= datetime('now', ?)"; params=(modifier,)
        row=self.conn.execute(f'SELECT count(*) calls, avg(success) rate, avg(latency_ms) ttft, sum(cost) total_cost FROM observation WHERE {where}',params).fetchone()
        accounting = self.accounting(window=window)
        fees = accounting['fee_buckets']
        total_cost = next(iter(fees.values()), {}).get('total_fee') if len(fees) == 1 else None
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
        attempts_by_route: dict[str, list[dict]] = {}
        usage_by_route: dict[str, list[dict]] = {}
        for offset in range(0, len(route_ids), 500):
            batch = route_ids[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            for item in self.conn.execute(
                f"SELECT route_id,site_id,role,status,elapsed_ms FROM route_attempt WHERE route_id IN ({placeholders}) ORDER BY route_id,attempt_number",
                batch,
            ).fetchall():
                attempts_by_route.setdefault(item["route_id"], []).append(dict(item))
            for item in self.conn.execute(
                f"""SELECT route_id,fingerprint,status,input_tokens,output_tokens,cost FROM observation
                    WHERE route_id IN ({placeholders}) AND attempt_number IS NOT NULL AND attempt_number>0""",
                batch,
            ).fetchall():
                usage_by_route.setdefault(item["route_id"], []).append(dict(item))
        amplifications = [self._amplification_from_rows(attempts_by_route.get(route_id, []), usage_by_route.get(route_id, [])) for route_id in route_ids]
        attempts = sorted(item["attempts_started"] for item in amplifications)
        hedges = [item for item in amplifications if item["hedge_started"]]
        costs_known = [item for item in amplifications if item["cost_attempts"]]
        return {
            'calls': row['calls'], 'technical_success_rate': row['rate'], 'avg_ttft_ms': row['ttft'], 'p95_ttft_ms': p95,
            'total_cost': total_cost, 'fee_buckets': fees, 'accounting': accounting,
            'model_fulfillment_rate': fulfillment, 'failures': failures,
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
            'input_tokens':r['input_tokens'],'output_tokens':r['output_tokens'],'cost':r['cost'],'currency':r['currency'],
            'request_id':r['request_id'],'route_id':r['route_id'],'attempt_number':r['attempt_number'],
            'started_ms':r['started_ms'],'elapsed_ms':r['elapsed_ms'],
            'diagnostic':json.loads(r['diagnostic_json']) if r['diagnostic_json'] else {},
        } for r in rows]
