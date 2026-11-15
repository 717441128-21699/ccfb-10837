"""覆盖性采集：挑选多类根公司发起穿透任务，为全量核对铺数据。"""
from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import sqlite3

from equity.config import SETTINGS
from equity.crawler.accounts import AccountPool
from equity.crawler.client import SimClient
from equity.crawler.engine import CrawlEngine
from equity.crawler.seed import init_warehouse, seed_accounts
from equity.db import AsyncDB


def pick_roots() -> list[str]:
    sim = sqlite3.connect(SETTINGS.sim_db)
    sim.row_factory = sqlite3.Row
    roots: list[str] = []
    # 1) 三家纯环公司
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM companies WHERE name LIKE '环宇%' ORDER BY name")]
    # 2) 全部代持公司
    roots += [r["company_id"] for r in sim.execute(
        "SELECT DISTINCT company_id FROM holding_events WHERE is_nominee=1")]
    # 3) 表决权安排公司（事件 note 含一致行动）
    roots += [r["company_id"] for r in sim.execute(
        "SELECT DISTINCT company_id FROM holding_events "
        "WHERE note LIKE '%一致行动%' LIMIT 5")]
    # 4) 集团顶层（创始人直接 67%）
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM holdings WHERE holder_kind='person' "
        "AND shares_pct>=67 GROUP BY company_id LIMIT 12")]
    # 5) 共同控制公司
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM preset_controllers WHERE controller_type='joint' "
        "GROUP BY company_id LIMIT 8")]
    # 6) 无控制公司
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM preset_controllers WHERE controller_type='none' "
        "GROUP BY company_id LIMIT 8")]
    # 7) 变更记录最多的公司
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM holding_events GROUP BY company_id "
        "ORDER BY COUNT(*) DESC LIMIT 6")]
    # 8) 随机散点
    roots += [r["company_id"] for r in sim.execute(
        "SELECT company_id FROM companies ORDER BY RANDOM() LIMIT 12")]
    sim.close()
    # 去重保序
    seen: set[str] = set()
    return [x for x in roots if not (x in seen or seen.add(x))]


async def main(depth: int = 4) -> None:
    init_warehouse()
    seed_accounts()
    roots = pick_roots()
    print(f"[coverage] 选定 {len(roots)} 个根公司，深度 {depth}")
    db = AsyncDB(SETTINGS.warehouse_db)
    pool = AccountPool(db, SETTINGS.sim_base_url)
    await pool.start()
    try:
        engine = CrawlEngine(db, pool, concurrency=6)
        async with SimClient(pool) as client:
            for i, cid in enumerate(roots, 1):
                task_id = await engine.create_task(cid, depth)
                stats = await engine.run_task(task_id, client)
                print(f"[coverage] {i}/{len(roots)} 根 {cid} 完成："
                      f"抓 {stats.fetched} 缓存 {stats.from_cache} "
                      f"公司 {stats.companies_seen} 错误 {len(stats.errors)}")
        snap = await pool.snapshot()
        total_used = sum(s["used"] for s in snap)
        print(f"[coverage] 配额总消耗 {total_used} 次")
        count = (await db.query_one("SELECT COUNT(*) n FROM companies"))["n"]
        print(f"[coverage] 仓库公司数 {count}")
    finally:
        await pool.close()
        db.close()


if __name__ == "__main__":
    import sys
    d = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    asyncio.run(main(d))
