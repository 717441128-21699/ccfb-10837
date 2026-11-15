"""全量采集：把仿真站全部公司放入一个任务的队列，逐家抓完。

用于验收前的数据铺底。请求总量 ≈ 公司数 + 1 次搜索，
5 账号 × 800 的当日配额（4000）足够覆盖 3000 家。
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import sqlite3
import time

from equity.config import SETTINGS
from equity.crawler.accounts import AccountPool
from equity.crawler.client import SimClient
from equity.crawler.engine import CrawlEngine
from equity.crawler.seed import init_warehouse, seed_accounts
from equity.db import AsyncDB


async def main() -> None:
    init_warehouse()
    seed_accounts()
    sim = sqlite3.connect(SETTINGS.sim_db)
    all_ids = [r[0] for r in sim.execute(
        "SELECT company_id FROM companies ORDER BY company_id")]
    sim.close()
    print(f"[full] 仿真站公司总数 {len(all_ids)}")

    db = AsyncDB(SETTINGS.warehouse_db)
    pool = AccountPool(db, SETTINGS.sim_base_url)
    await pool.start()
    try:
        engine = CrawlEngine(db, pool, concurrency=8)
        task_id = await engine.create_task(all_ids[0], max_depth=99)
        # 直接把全部公司灌进该任务队列（depth=0，max_depth 很大不会被截断）
        for cid in all_ids:
            await db.execute(
                "INSERT OR IGNORE INTO task_queue(task_id,company_id,depth,status)"
                " VALUES (?,?,0,'queued')", (task_id, cid))
        t0 = time.time()
        async with SimClient(pool) as client:
            stats = await engine.run_task(task_id, client)
        elapsed = time.time() - t0
        print(f"[full] 完成 用时 {elapsed:.1f}s")
        print(f"[full] 抓取 {stats.fetched} 缓存 {stats.from_cache} "
              f"自然人 {stats.persons_seen} 错误 {len(stats.errors)}")
        if stats.errors:
            print("[full] 前 5 个错误:", stats.errors[:5])
        n = (await db.query_one("SELECT COUNT(*) n FROM companies"))["n"]
        print(f"[full] 仓库公司数 {n} / 目标 {len(all_ids)}")
        for s in await pool.snapshot():
            print(f"  配额 {s['account_id']}: {s['used']}/{s['daily_quota']} "
                  f"耗尽={s['exhausted']}")
    finally:
        await pool.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
