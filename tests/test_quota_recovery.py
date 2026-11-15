"""验证多账号轮用 + 配额耗尽自动歇到次日恢复（需求 2，确定性强测）。

- 用独立的临时仓库，保证全是真实请求、无缓存；
- 直接把仿真站 sim_accounts 当日额度调小并清零用量（服务端逐请求读库，立即生效）；
- 采集量超过“账号数 × 小额度”，必然全部 429；
- 全部耗尽后调用仿真站管理接口推进一个虚拟日（模拟“第二天到来”），
  验证采集器靠 health 轮询自动感知翻日、自动复活账号并把任务跑完，全程无重启。
结束后恢复仿真站原始额度。
"""
from __future__ import annotations

import _bootstrap  # noqa: F401

import asyncio
import os
import tempfile
from pathlib import Path

import aiohttp

from equity.config import SETTINGS
from equity.db import connect
from equity.crawler.accounts import AccountPool
from equity.crawler.client import SimClient
from equity.crawler.engine import CrawlEngine
from equity.crawler.seed import init_warehouse, seed_accounts
from equity.db import AsyncDB

SMALL_QUOTA = 6


async def admin_post(path: str, body: dict | None = None) -> dict:
    headers = {"X-Admin-Token": SETTINGS.admin_token}
    async with aiohttp.ClientSession() as s:
        async with s.post(SETTINGS.sim_base_url + path, headers=headers,
                          json=body or {}) as r:
            return await r.json()


def set_sim_quota(quota: int) -> None:
    conn = connect(SETTINGS.sim_db)
    conn.execute("UPDATE sim_accounts SET daily_quota=?", (quota,))
    conn.execute("DELETE FROM sim_quota_usage")
    conn.commit()
    conn.close()


def gsim_query_ids(n: int) -> list[tuple[str]]:
    conn = connect(SETTINGS.sim_db)
    rows = conn.execute(
        "SELECT company_id FROM companies ORDER BY company_id LIMIT ?",
        (n,)).fetchall()
    conn.close()
    return [(r["company_id"],) for r in rows]


async def main() -> None:
    # 备份仿真站原始额度
    sim = connect(SETTINGS.sim_db)
    original = {r["account_id"]: r["daily_quota"]
                for r in sim.execute("SELECT account_id,daily_quota "
                                     "FROM sim_accounts")}
    sim.close()

    tmp_dir = Path(tempfile.mkdtemp(prefix="eqs_quota_"))
    warehouse = tmp_dir / "quota_test.db"

    set_sim_quota(SMALL_QUOTA)
    await admin_post("/admin/reset-quota")

    # 独立空仓库：手动建表并同步小额度账号（不走全局 SETTINGS 的种子路径）
    init_warehouse(str(warehouse))
    gsim = connect(SETTINGS.sim_db)
    wh = connect(str(warehouse))
    for r in gsim.execute(
            "SELECT account_id,api_key,daily_quota FROM sim_accounts"):
        wh.execute(
            "INSERT INTO accounts(account_id,api_key,base_url,daily_quota,"
            "enabled,note) VALUES (?,?,?,? ,1,'t')",
            (r["account_id"], r["api_key"], SETTINGS.sim_base_url,
             r["daily_quota"]))
    wh.commit()
    wh.close()
    gsim.close()

    db = AsyncDB(str(warehouse))
    pool = AccountPool(db, SETTINGS.sim_base_url, refresh_interval=3)
    await pool.start()
    engine = CrawlEngine(db, pool, concurrency=4)
    task_id = await engine.create_task("C000003", max_depth=6)
    # 显式灌入 45 家公司（> 5账号×6额度=30），保证必然跨虚拟日；
    # 不依赖单根向上 BFS 的子树大小
    sim_ids = [r[0] for r in gsim_query_ids(45)]
    for cid in sim_ids:
        await db.execute(
            "INSERT OR IGNORE INTO task_queue(task_id,company_id,depth,status)"
            " VALUES (?,?,0,'queued')", (task_id, cid))

    day_advanced = False
    exhaustion_seen = asyncio.Event()

    async def watcher():
        nonlocal day_advanced
        while True:
            await asyncio.sleep(1)
            snap = await pool.snapshot()
            exhausted = [s for s in snap if s["exhausted"]]
            if exhausted:
                exhaustion_seen.set()
            # 一旦观察到耗尽且尚未推进，立即模拟“第二天到来”
            if exhausted and not day_advanced:
                print(f"  [观察] {len(exhausted)} 个账号耗尽，推进虚拟日…",
                      flush=True)
                await admin_post("/admin/clock", {"days": 1})
                await admin_post("/admin/reset-quota")
                day_advanced = True

    w = asyncio.create_task(watcher())
    try:
        async with SimClient(pool) as client:
            stats = await engine.run_task(task_id, client)
    finally:
        w.cancel()
        await pool.close()
        db.close()

    # 恢复仿真站额度与用量
    conn = connect(SETTINGS.sim_db)
    for aid, q in original.items():
        conn.execute("UPDATE sim_accounts SET daily_quota=? WHERE account_id=?",
                     (q, aid))
    conn.execute("DELETE FROM sim_quota_usage")
    conn.commit()
    conn.close()

    print("真实请求抓取:", stats.fetched, "缓存:", stats.from_cache,
          "错误:", len(stats.errors))
    print("是否观察到耗尽:", exhaustion_seen.is_set(),
          "是否跨虚拟日恢复:", day_advanced)
    assert exhaustion_seen.is_set(), "应至少观察到一个账号被 429 耗尽"
    assert day_advanced, "应触发虚拟日推进"
    assert stats.fetched > SMALL_QUOTA * 5, \
        "跨日恢复后抓取量应超过单虚拟日总配额"
    assert not stats.errors, "任务不应有错误"
    print("PASS: 多账号轮用 → 全部 429 歇菜 → 虚拟日翻页自动复活 → 任务跑完")


if __name__ == "__main__":
    asyncio.run(main())
