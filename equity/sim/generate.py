"""把 NetworkBuilder 的内存关系网落盘成仿真公示站数据库。"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from ..config import SETTINGS
from ..db import connect
from ..analysis.history import replay_events, assert_cap_table_closed
from .schema import SIM_SCHEMA
from .world import NetworkBuilder, TODAY


def _rebuild(db_path: str) -> sqlite3.Connection:
    p = Path(db_path)
    if p.exists():
        p.unlink()
    for suffix in ("-wal", "-shm"):
        extra = Path(str(p) + suffix)
        if extra.exists():
            extra.unlink()
    conn = connect(p)
    conn.executescript(SIM_SCHEMA)
    return conn


def generate(db_path: str | None = None, *, target: int | None = None,
             seed: int | None = None) -> dict:
    db_path = db_path or SETTINGS.sim_db
    target = target or SETTINGS.company_count
    seed = seed if seed is not None else SETTINGS.rng_seed

    builder = NetworkBuilder(target=target, seed=seed).build()
    conn = _rebuild(db_path)
    cur = conn.cursor()

    # ---- 公司
    cur.executemany(
        "INSERT INTO companies(company_id,name,credit_code,established_date,"
        "registered_capital,industry,region,profile_mode,source_note) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        [(c.company_id, c.name, c.credit_code, c.established_date,
          c.registered_capital, c.industry, c.region, c.profile_mode,
          c.preset_rationale)
         for c in builder.companies.values()],
    )
    # ---- 别名
    cur.executemany(
        "INSERT INTO company_aliases(company_id,alias) VALUES (?,?)",
        [(c.company_id, alias) for c in builder.companies.values()
         for alias in c.aliases],
    )
    # ---- 自然人
    cur.executemany(
        "INSERT INTO persons(person_id,name,id_tail,gender) VALUES (?,?,?,?)",
        [(p.person_id, p.name, p.id_tail, p.gender)
         for p in builder.persons.values()],
    )
    # ---- 事件（change_no 按公司内时间顺序编号）
    by_company: dict[str, list] = {}
    for ev in builder.events:
        by_company.setdefault(ev.company_id, []).append(ev)

    # 全局校验：每家公司都必须有股东事件，且当前时点股东表非空、闭合到 100%
    missing = [cid for cid in builder.companies if cid not in by_company]
    if missing:
        raise AssertionError(f"{len(missing)} 家公司没有任何股东事件: {missing[:5]}")
    event_rows = []
    snapshot_rows = []
    for cid, evs in by_company.items():
        evs.sort(key=lambda e: (e.event_date, e.holder_id))
        first_date = evs[0].event_date
        for no, ev in enumerate(evs):
            event_rows.append(
                (cid, ev.holder_kind, ev.holder_id, ev.event_date, ev.valid_to,
                 ev.shares_pct, ev.voting_pct, ev.is_nominee,
                 ev.beneficial_holder_id, no, ev.note))
        # 用与采集端相同的回放函数推当前股东，顺便做闭合校验
        current = replay_events(evs)
        if not current:
            raise AssertionError(f"{cid} 回放得到空股东表")
        assert_cap_table_closed(current)
        for h in current:
            snapshot_rows.append(
                (cid, h["holder_kind"], h["holder_id"], h["shares_pct"],
                 h["voting_pct"], h["is_nominee"], h["beneficial_holder_id"]))
    cur.executemany(
        "INSERT INTO holding_events(company_id,holder_kind,holder_id,event_date,"
        "valid_to,shares_pct,voting_pct,is_nominee,beneficial_holder_id,change_no,note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)", event_rows)
    cur.executemany(
        "INSERT INTO holdings(company_id,holder_kind,holder_id,shares_pct,"
        "voting_pct,is_nominee,beneficial_holder_id) VALUES (?,?,?,?,?,?,?)",
        snapshot_rows)
    # ---- 预设实控人答案
    preset_rows = []
    rank = 0
    for c in builder.companies.values():
        if c.preset_type == "joint" and c.preset_controller_id:
            for no, pid in enumerate(c.preset_controller_id.split("|")):
                preset_rows.append((c.company_id, "joint", pid,
                                    c.preset_rationale, no))
        elif c.preset_type == "person" and c.preset_controller_id:
            preset_rows.append((c.company_id, "person", c.preset_controller_id,
                                c.preset_rationale, 0))
        else:
            preset_rows.append((c.company_id, "none", None,
                                c.preset_rationale or "无实际控制人", 0))
        rank += 1
    cur.executemany(
        "INSERT INTO preset_controllers(company_id,controller_type,controller_id,"
        "rationale,rank_no) VALUES (?,?,?,?,?)", preset_rows)

    # ---- 账号
    account_rows = []
    for i in range(SETTINGS.account_count):
        aid = f"A{i + 1:02d}"
        account_rows.append((aid, f"key-{aid}-secret", f"调研账号{i + 1}",
                             SETTINGS.daily_quota))
    cur.executemany(
        "INSERT INTO sim_accounts(account_id,api_key,owner_name,daily_quota)"
        " VALUES (?,?,?,?)", account_rows)

    # ---- 元信息
    meta = {
        "generated_at": TODAY.isoformat(),
        "seed": str(seed),
        "company_count": str(len(builder.companies)),
        "person_count": str(len(builder.persons)),
        "event_count": str(len(builder.events)),
        "alias_count": str(sum(len(c.aliases) for c in builder.companies.values())),
        "nominee_company_count": str(len({
            e.company_id for e in builder.events if e.is_nominee})),
        "duplicate_name_count": str(sum(
            1 for ids in builder.duplicate_names.values() if len(ids) > 1)),
    }
    cur.executemany("INSERT INTO sim_meta(key,value) VALUES (?,?)",
                    list(meta.items()))
    conn.commit()
    conn.close()

    # 虚拟时钟初始文件
    clock = Path(SETTINGS.clock_state)
    if not clock.exists():
        clock.write_text(json.dumps(
            {"day": TODAY.isoformat(), "epoch": 0}, ensure_ascii=False),
            encoding="utf-8")
    return meta


if __name__ == "__main__":
    info = generate()
    print(json.dumps(info, ensure_ascii=False, indent=2))
