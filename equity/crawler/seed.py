"""采集仓库初始化 + 账号种子（从仿真站账号表同步到本地账号表）。"""
from __future__ import annotations

import sqlite3

from ..config import SETTINGS
from ..db import connect
from .schema_warehouse import WAREHOUSE_SCHEMA


def init_warehouse(db_path: str | None = None) -> None:
    conn = connect(db_path or SETTINGS.warehouse_db)
    conn.executescript(WAREHOUSE_SCHEMA)
    # 默认规则配置（每次初始化都刷新，保证与代码里的默认口径一致）
    import json
    from datetime import date
    conn.execute("DELETE FROM rule_configs WHERE name='默认规则'")
    conn.execute(
        "INSERT INTO rule_configs(name,rules_json,is_active,created_at) "
        "VALUES (?,?,1,?)",
        ("默认规则", json.dumps(SETTINGS.default_rules, ensure_ascii=False),
         date.today().isoformat()))
    conn.commit()
    conn.close()


def seed_accounts() -> int:
    """把仿真站账号同步到本地采集账号表（保留 enabled 状态）。"""
    init_warehouse()
    sim = sqlite3.connect(SETTINGS.sim_db)
    sim.row_factory = sqlite3.Row
    rows = sim.execute(
        "SELECT account_id, api_key, daily_quota FROM sim_accounts "
        "ORDER BY account_id").fetchall()
    sim.close()

    wh = connect(SETTINGS.warehouse_db)
    n = 0
    for r in rows:
        exists = wh.execute(
            "SELECT 1 FROM accounts WHERE account_id=?", (r["account_id"],)).fetchone()
        if exists:
            wh.execute(
                "UPDATE accounts SET api_key=?, daily_quota=? WHERE account_id=?",
                (r["api_key"], r["daily_quota"], r["account_id"]))
        else:
            wh.execute(
                "INSERT INTO accounts(account_id,api_key,base_url,daily_quota,"
                "enabled,note) VALUES (?,?,?,? ,1,?)",
                (r["account_id"], r["api_key"], SETTINGS.sim_base_url,
                 r["daily_quota"], f"种子账号 {r['account_id']}"))
        n += 1
    wh.commit()
    wh.close()
    return n


if __name__ == "__main__":
    init_warehouse()
    count = seed_accounts()
    print(f"仓库已初始化，同步账号 {count} 个 -> {SETTINGS.warehouse_db}")
