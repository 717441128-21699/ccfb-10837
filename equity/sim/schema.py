"""仿真公示站与采集仓库的建表语句。"""
from __future__ import annotations

SIM_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id        TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    credit_code       TEXT NOT NULL UNIQUE,
    established_date  TEXT NOT NULL,
    registered_capital REAL NOT NULL,
    industry          TEXT NOT NULL,
    region            TEXT NOT NULL,
    profile_mode      TEXT NOT NULL CHECK (profile_mode IN ('direct','change')),
    source_note       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS company_aliases (
    company_id  TEXT NOT NULL,
    alias       TEXT NOT NULL,
    PRIMARY KEY (company_id, alias)
);

CREATE TABLE IF NOT EXISTS persons (
    person_id  TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    id_tail    TEXT NOT NULL,
    gender     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS holding_events (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id    TEXT NOT NULL,
    holder_kind   TEXT NOT NULL CHECK (holder_kind IN ('company','person')),
    holder_id     TEXT NOT NULL,
    event_date    TEXT NOT NULL,
    valid_to      TEXT,
    shares_pct    REAL NOT NULL,
    voting_pct    REAL NOT NULL,
    is_nominee    INTEGER NOT NULL DEFAULT 0,
    beneficial_holder_id TEXT,
    change_no     INTEGER NOT NULL DEFAULT 0,
    note          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_he_company ON holding_events(company_id, event_date);

-- 当前快照（由事件回放得到，direct 模式详情页直接返回它）
CREATE TABLE IF NOT EXISTS holdings (
    company_id  TEXT NOT NULL,
    holder_kind TEXT NOT NULL,
    holder_id   TEXT NOT NULL,
    shares_pct  REAL NOT NULL,
    voting_pct  REAL NOT NULL,
    is_nominee  INTEGER NOT NULL DEFAULT 0,
    beneficial_holder_id TEXT,
    PRIMARY KEY (company_id, holder_kind, holder_id)
);

CREATE TABLE IF NOT EXISTS preset_controllers (
    company_id      TEXT NOT NULL,
    controller_type TEXT NOT NULL CHECK (controller_type IN ('person','joint','none')),
    controller_id   TEXT,
    rationale       TEXT NOT NULL DEFAULT '',
    rank_no         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_preset_company ON preset_controllers(company_id);

CREATE TABLE IF NOT EXISTS sim_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_accounts (
    account_id   TEXT PRIMARY KEY,
    api_key      TEXT NOT NULL UNIQUE,
    owner_name   TEXT NOT NULL,
    daily_quota  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sim_quota_usage (
    account_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day)
);
"""

WAREHOUSE_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    company_id       TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    credit_code      TEXT,
    normalized_name  TEXT NOT NULL,
    established_date TEXT,
    industry         TEXT,
    region           TEXT,
    profile_mode     TEXT,
    first_task_id    INTEGER,
    discovered_at    TEXT NOT NULL,
    last_synced_at   TEXT,
    UNIQUE (credit_code)
);
CREATE INDEX IF NOT EXISTS idx_c_normname ON companies(normalized_name);

CREATE TABLE IF NOT EXISTS company_aliases (
    company_id TEXT NOT NULL,
    alias      TEXT NOT NULL,
    PRIMARY KEY (company_id, alias)
);

CREATE TABLE IF NOT EXISTS persons (
    person_id TEXT PRIMARY KEY,
    name      TEXT NOT NULL,
    id_tail   TEXT
);

CREATE TABLE IF NOT EXISTS holdings_raw (
    company_id  TEXT NOT NULL,
    holder_kind TEXT NOT NULL,
    holder_id   TEXT NOT NULL,
    holder_name TEXT NOT NULL,
    holder_id_tail TEXT,
    shares_pct  REAL NOT NULL,
    voting_pct  REAL NOT NULL,
    is_nominee  INTEGER NOT NULL DEFAULT 0,
    beneficial_holder_id TEXT,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (company_id, holder_kind, holder_id)
);

CREATE TABLE IF NOT EXISTS holding_events_raw (
    event_id     INTEGER PRIMARY KEY,
    company_id   TEXT NOT NULL,
    holder_kind  TEXT NOT NULL,
    holder_id    TEXT NOT NULL,
    holder_name  TEXT NOT NULL,
    event_date   TEXT NOT NULL,
    valid_to     TEXT,
    shares_pct   REAL NOT NULL,
    voting_pct   REAL NOT NULL,
    is_nominee   INTEGER NOT NULL DEFAULT 0,
    beneficial_holder_id TEXT,
    change_no    INTEGER NOT NULL DEFAULT 0,
    note         TEXT NOT NULL DEFAULT '',
    fetched_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_her_company ON holding_events_raw(company_id, event_date);

CREATE TABLE IF NOT EXISTS crawl_tasks (
    task_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    root_query   TEXT NOT NULL,
    root_company_id TEXT,
    max_depth    INTEGER NOT NULL,
    follow_nominee INTEGER NOT NULL DEFAULT 1,
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','running','done','failed','stopped')),
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    error        TEXT,
    stats_json   TEXT
);

CREATE TABLE IF NOT EXISTS task_queue (
    task_id    INTEGER NOT NULL,
    company_id TEXT NOT NULL,
    depth      INTEGER NOT NULL,
    status     TEXT NOT NULL DEFAULT 'queued'
               CHECK (status IN ('queued','done','skipped_depth','skipped_seen')),
    PRIMARY KEY (task_id, company_id)
);

CREATE TABLE IF NOT EXISTS accounts (
    account_id   TEXT PRIMARY KEY,
    api_key      TEXT NOT NULL,
    base_url     TEXT NOT NULL,
    daily_quota  INTEGER NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    note         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS quota_usage (
    account_id TEXT NOT NULL,
    day        TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_id, day)
);

CREATE TABLE IF NOT EXISTS rule_configs (
    config_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    rules_json  TEXT NOT NULL,
    is_active   INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS verify_reports (
    report_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    scope       TEXT NOT NULL,
    total       INTEGER NOT NULL,
    matched     INTEGER NOT NULL,
    mismatched  INTEGER NOT NULL,
    unknown     INTEGER NOT NULL,
    detail_json TEXT NOT NULL
);
"""
