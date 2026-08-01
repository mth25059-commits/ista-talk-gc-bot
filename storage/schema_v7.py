"""
Eve v7 schema — naye tables (PEOPLE, GC_PROFILE, TRIGGERS, API_KEYS, TG_STATE).

Purane V6 tables ko haath nahi lagate. Ye sirf ADD karta hai, isliye
migration safe hai — koi data loss nahi.

Boot pe ek baar call karo:

    from storage.database import init_db
    from storage.schema_v7 import ensure_v7_schema
    init_db()
    ensure_v7_schema()
"""
from __future__ import annotations

import logging

from storage.database import get_connection

logger = logging.getLogger("eve.schema7")

SCHEMA_VERSION = 7

_DDL = """
-- ---------------------------------------------------------------- PEOPLE
-- Har banda jise bot jaanta hai. Manual entry (TG se) + auto-learn (GC se).
CREATE TABLE IF NOT EXISTS PEOPLE (
    ig_username     TEXT PRIMARY KEY,          -- lowercase, bina @
    ig_user_id      TEXT,                      -- resolve hone pe bhar jayega
    real_name       TEXT,                      -- "Rahul"
    gender          TEXT DEFAULT 'unknown',    -- boy | girl | unknown
    relation        TEXT DEFAULT 'stranger',   -- admin | friend | stranger | enemy
    tone_learned    TEXT,                      -- kaise baat karta hai (auto)
    tone_override   TEXT,                      -- admin ne force kiya tone
    notes           TEXT,                      -- free text memory
    source          TEXT DEFAULT 'auto',       -- manual | auto
    confidence      REAL DEFAULT 0.5,
    msg_count       INTEGER DEFAULT 0,
    intro_asked     INTEGER DEFAULT 0,         -- "intro de" ek hi baar poochho
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_people_userid ON PEOPLE(ig_user_id);
CREATE INDEX IF NOT EXISTS idx_people_relation ON PEOPLE(relation);

-- ------------------------------------------------------------ GC_PROFILE
-- Har group ka apna lehja. Bot yahan se seekhta hai ki is GC me kaise bolna.
CREATE TABLE IF NOT EXISTS GC_PROFILE (
    thread_id       TEXT PRIMARY KEY,
    title           TEXT,
    gali_level      REAL DEFAULT 0.0,      -- 0-10
    flirty_level    REAL DEFAULT 0.0,      -- 0-10
    friendly_level  REAL DEFAULT 5.0,      -- 0-10
    toxic_level     REAL DEFAULT 0.0,      -- 0-10
    avg_msg_len     REAL DEFAULT 0.0,
    slang_json      TEXT DEFAULT '[]',     -- top slang words
    sample_count    INTEGER DEFAULT 0,     -- kitne msgs pe base hai
    last_learned_at TEXT,
    updated_at      TEXT NOT NULL
);

-- -------------------------------------------------------------- TRIGGERS
-- Fixed-tone triggers: ye username bole -> hamesha is tone me reply.
CREATE TABLE IF NOT EXISTS TRIGGERS (
    ig_username     TEXT PRIMARY KEY,
    tone            TEXT NOT NULL,         -- roast|dirty|flirty|abusive_roast|friendly|sarcastic
    active          INTEGER DEFAULT 1,
    hit_count       INTEGER DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- -------------------------------------------------------------- API_KEYS
-- Unlimited keys per provider, round-robin with per-key quota + failover.
CREATE TABLE IF NOT EXISTS API_KEYS (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,         -- groq|xai|gemini|anthropic|agentrouter
    label           TEXT,
    api_key         TEXT NOT NULL,
    model           TEXT,                  -- is key se kaunsa model chalana hai
    verified_at     TEXT,                  -- last successful live check
    base_url        TEXT,                  -- optional (agentrouter etc.)
    position        INTEGER NOT NULL DEFAULT 0,
    quota_limit     INTEGER NOT NULL DEFAULT 100,
    quota_used      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active',   -- active | exhausted | dead
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_used_at    TEXT,
    created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_apikeys_unique ON API_KEYS(provider, api_key);
CREATE INDEX IF NOT EXISTS idx_apikeys_pick ON API_KEYS(provider, status, position);

-- -------------------------------------------------------------- TG_STATE
-- Telegram panel ka apna state (update_id, pending prompts, wizard steps).
CREATE TABLE IF NOT EXISTS TG_STATE (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


# Purane v7 DB me jo columns baad me add hue — safe ALTER (idempotent).
_ADD_COLUMNS = {
    "API_KEYS": {
        "model": "TEXT",
        "verified_at": "TEXT",
    },
}


def _ensure_columns(conn) -> None:
    for table, cols in _ADD_COLUMNS.items():
        try:
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        except Exception:
            continue
        if not have:
            continue
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
                logger.info("[SCHEMA] %s.%s add kiya", table, col)


def ensure_v7_schema() -> None:
    """Idempotent. Har boot pe safely call kar sakta hai."""
    with get_connection() as conn:
        conn.executescript(_DDL)
        _ensure_columns(conn)
        conn.execute(
            """
            INSERT INTO BOT_STATE (key, value, updated_at)
            VALUES ('schema_version', ?, datetime('now'))
            ON CONFLICT(key) DO UPDATE SET value = excluded.value,
                                           updated_at = excluded.updated_at
            """,
            (str(SCHEMA_VERSION),),
        )
    logger.info("[SCHEMA] v%d ensured", SCHEMA_VERSION)

